from confluent_kafka import DeserializingConsumer, TopicPartition
from confluent_kafka.schema_registry.avro import AvroDeserializer
from .custom_exceptions import NoMessageError, ConsumeMessageError, FinishedTransactionBatch
from .general_utils import parse_headers, get_guid_from_message
from copy import deepcopy
import logging
import datetime

LOGGER = logging.getLogger(__name__)


class Consumer:
    def __init__(self, urls, group_id, consume_topics_list, schema_registry=None, auto_subscribe=True, client_auth_config=None, settings_config=None, metrics_manager=None):
        self._urls = ','.join(urls) if isinstance(urls, list) else urls
        self._auth = client_auth_config
        self._settings = settings_config
        self._consumer = None
        self._group_id = group_id
        self._topic_metadata = None
        self._schema_registry = schema_registry

        self.message = None
        self.metrics_manager = metrics_manager
        self.topics = consume_topics_list if isinstance(consume_topics_list, list) else consume_topics_list.split(',')
        self._poll_timeout = self._settings.poll_timeout_secs if self._settings else 5

        self._init_consumer(auto_subscribe=auto_subscribe)

    def __getattr__(self, attr):
        """Note: this includes methods as well!"""
        try:
            return self.__getattribute__(attr)
        except AttributeError:
            return self._consumer.__getattribute__(attr)

    @staticmethod
    def _consume_message_callback(error, partitions):
        """
        Logs the info returned when a successful commit is performed
        NOTE: Callback requires these args
        """
        if error:
            LOGGER.critical(error)
        else:
            LOGGER.debug('Consumer Callback - Message consumption committed successfully')

    def _make_config(self):
        settings = {
            "bootstrap.servers": self._urls,

            "group.id": self._group_id,
            "on_commit": self._consume_message_callback,
            "enable.auto.offset.store": False,  # ensures auto-committing doesn't happen before the consumed message is actually finished
            "partition.assignment.strategy": 'cooperative-sticky',

            # Registry Serialization Settings
            "key.deserializer": AvroDeserializer(self._schema_registry, schema_str='{"type": "string"}'),
            "value.deserializer": AvroDeserializer(self._schema_registry) if self._schema_registry else None,
        }

        if self._settings:
            settings.update(self._settings.as_client_dict())
        if self._auth:
            settings.update(self._auth.as_client_dict())
        return settings

    def _init_consumer(self, auto_subscribe=True):
        LOGGER.debug('Initializing Consumer...')
        self._consumer = DeserializingConsumer(self._make_config())
        LOGGER.info('Consumer Initialized!')
        if auto_subscribe:
            self._consumer.subscribe(topics=self.topics)
            LOGGER.info(f'Consumer subscribed to topics {self.topics}!')

    def _poll_for_message(self, timeout=None):
        """Is a separate method to isolate communication interface with Kafka"""
        if not timeout:
            timeout = self._poll_timeout
        message = self._consumer.poll(timeout)
        if message is None:
            raise NoMessageError
        return message

    def _handle_consumed_message(self):
        """
        Handles a consumed message to check for errors and log the consumption as a metric.
        If the message is returned with a breaking error, raises a ConsumeMessageError.
        """
        try:
            guid = get_guid_from_message(self.message)
            if self.metrics_manager:
                self.metrics_manager.set_seconds_behind(
                    round(datetime.datetime.timestamp(datetime.datetime.utcnow())) - self.message.timestamp()[1] // 1000)
            if '__changelog' not in self.message.topic():
                LOGGER.info(
                    f"Message consumed from topic {self.message.topic()} partition {self.message.partition()}, offset {self.message.offset()}; GUID {guid}")
                LOGGER.debug(f"Consumed message key: {repr(self.message.key())}")
        except AttributeError:
            if "object has no attribute 'headers'" in str(self.message.error()):
                raise ConsumeMessageError("Headers were inaccessible on the message. Potentially a corrupt message?")

        # Increment the metric for consumed messages by one
        if self.metrics_manager:
            self.metrics_manager.inc_messages_consumed(1, self.message.topic())

    def key(self):
        return deepcopy(self.message.key())

    def value(self):
        return deepcopy(self.message.value())

    def headers(self):
        return deepcopy(parse_headers(self.message.headers()))

    def messages(self):
        return [self.message]

    def consume(self, timeout=None):
        """
        Consumes a message from the broker while handling errors.
        If the message is valid, then the message is returned.
        """
        self.message = self._poll_for_message(timeout)
        self._handle_consumed_message()
        return self.message

    def commit(self):
        self._consumer.store_offsets(self.message)
        self.message = None


class TransactionalConsumer(Consumer):
    def __init__(self, urls, group_id, consume_topics_list, schema_registry=None, auto_subscribe=True,
                 client_auth_config=None, settings_config=None, metrics_manager=None,
                 batch_consume_max_time_seconds=None, batch_consume_max_count=None, batch_consume_store_messages=None):
        super().__init__(urls, group_id, consume_topics_list, schema_registry=schema_registry, auto_subscribe=auto_subscribe,
                         client_auth_config=client_auth_config, settings_config=settings_config, metrics_manager=metrics_manager)

        # batch consuming
        if not batch_consume_max_time_seconds:
            batch_consume_max_time_seconds = self._settings.batch_consume_max_time_secs
        if not batch_consume_max_count:
            batch_consume_max_count = self._settings.batch_consume_max_count
        if not batch_consume_store_messages:
            batch_consume_store_messages = self._settings.batch_consume_store_messages
        self._batch_time_elapse_start = None
        self._consume_max_time_secs = batch_consume_max_time_seconds
        self._consume_max_count = batch_consume_max_count
        self._store_batch_messages = batch_consume_store_messages
        self._init_attrs()

    def _init_attrs(self):
        self._batch_offset_starts = {}
        self._batch_offset_ends = {}
        self._messages = []
        self._consume_message_count = 0
    
    def _set_batch_start_time(self):
        self._batch_time_elapse_start = datetime.datetime.now().timestamp()

    def _max_consume_time_continue(self):
        continue_consume = True
        if self._consume_max_time_secs:
            if not self._batch_time_elapse_start:
                self._set_batch_start_time()
            seconds_elapsed = datetime.datetime.now().timestamp() - self._batch_time_elapse_start
            continue_consume = seconds_elapsed < self._consume_max_time_secs
            if not continue_consume:
                self._batch_time_elapse_start = None
        return continue_consume

    def _max_consume_count_continue(self, consume_multiplier=1):
        if self._consume_max_count:
            return self._consume_message_count < (self._consume_max_count * consume_multiplier)
        return True

    def _keep_consuming(self, consume_multiplier=1):
        return self._max_consume_count_continue(consume_multiplier=consume_multiplier) and self._max_consume_time_continue()

    def _mark_offset_start(self):
        if self.message.topic() not in self._batch_offset_starts:
            self._batch_offset_starts[self.message.topic()] = {}
        if self.message.partition() not in self._batch_offset_starts[self.message.topic()]:
            self._batch_offset_starts[self.message.topic()][self.message.partition()] = self.message.offset()

    def _mark_offset_end(self):
        if self.message.topic() not in self._batch_offset_ends:
            self._batch_offset_ends[self.message.topic()] = {}
        self._batch_offset_ends[self.message.topic()][self.message.partition()] = self.message.offset()

    def _get_consumer_partition_assignment(self):
        assignments = self._consumer.assignment()
        assignments = {topic: [int(obj.partition) for obj in assignments if obj.topic == topic] for topic in set([obj.topic for obj in assignments])}
        return assignments

    def _commit(self, producer, offsets):
        if offsets:
            if not producer.active_transaction:
                producer.begin_transaction()
            producer.send_offsets_to_transaction(offsets, self._consumer.consumer_group_metadata())
        if producer.active_transaction:
            producer.commit_transaction(30)

    def _make_config(self):
        config = super()._make_config()
        config.update({
            "isolation.level": "read_committed",
            "enable.auto.commit": False,
            "enable.auto.offset.store": False,
        })
        return config

    def _handle_consumed_message(self):
        super()._handle_consumed_message()
        self._mark_offset_start()
        self._mark_offset_end()
        self._consume_message_count += 1
        if self._store_batch_messages:
            self._messages.append(self.message)

    @property
    def pending_commits(self):
        return bool(self._consume_message_count)

    def messages(self):
        if self._store_batch_messages:
            return self._messages
        return super().messages()

    def rollback_consumption(self):
        LOGGER.info('Rolling back consumer state to earliest non-committed offset(s)...')
        assignments = self._get_consumer_partition_assignment()
        for topic, partitions in self._batch_offset_starts.items():
            for partition, offset in partitions.items():
                if partition in assignments.get(topic, []):
                    LOGGER.info(f"Reversing topic {topic} partition {partition} back to offset {offset}")
                    self.consumer.seek(TopicPartition(topic=topic, partition=partition, offset=offset))
                    LOGGER.info(f"Consumer set topic {topic} partition {partition} to offset {self.consumer.position([TopicPartition(topic=topic, partition=partition)])[0].offset}")
        self._init_attrs()

    def commit(self, producer):
        offsets_to_commit = [TopicPartition(topic, partition, offset + 1) for topic, partitions in
                             self._batch_offset_ends.items() for partition, offset in partitions.items()]
        self._commit(producer, offsets_to_commit)
        self.message = None
        self._init_attrs()

    def consume(self, timeout=None, consume_multiplier=1):
        """
        Consumes a message from the broker while handling errors.
        If the message is valid, then the message is returned.
        """
        try:
            if self._keep_consuming(consume_multiplier=consume_multiplier):
                return super().consume(timeout=timeout)
            raise FinishedTransactionBatch
        except NoMessageError:
            if self._batch_offset_ends:
                raise FinishedTransactionBatch
            else:
                raise
