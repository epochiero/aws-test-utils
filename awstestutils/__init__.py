import boto3
import random
import json
import logging
import re


log = logging.getLogger('awstestutils')

TEST_NAME_PREFIX = 'test-'


def reduce_logging_output(level=logging.WARN):
    """Reduce the amount of boto related logging messages.

    Both boto3 and botocore can be quite verbose on DEBUG level. This helps
    reduce logging output when debugging other dependencies.
    """
    log.info('setting boto related logging to %s' % level)
    logging.getLogger('botocore').setLevel(level)
    logging.getLogger('boto3').setLevel(level)


def clean_test_queues(prefix=TEST_NAME_PREFIX):
    """Delete all queues that match a "test" name."""
    sqs = boto3.resource('sqs')
    num_queues = 0
    try:
        for queue in sqs.queues.all():
            if re.match(r'.+%s\d+' % TEST_NAME_PREFIX, queue.url):
                queue.delete()
                num_queues += 1
    finally:
        log.info('deleted %s test queues' % num_queues)


def clean_test_topics(prefix=TEST_NAME_PREFIX):
    """Delete all topics that match a "test" name."""
    sns = boto3.resource('sns')
    num_topics = 0
    try:
        for topic in sns.topics.all():
            if re.match(r'.+%s\d+' % TEST_NAME_PREFIX, topic.arn):
                topic.delete()
                num_topics += 1
    finally:
        log.info('deleted %s test topics' % num_topics)


def cleanup(prefix=TEST_NAME_PREFIX):
    """Delete topics and queues that match a "test" name.

    The documentation for boto3 states: "If you delete a queue, you must wait
    at least 60 seconds before creating a queue with the same name". This delay
    applies to this function as well.
    """
    log.info('checking for left over test queues')
    clean_test_queues(prefix)
    log.info('checking for left over test queues')
    clean_test_topics(prefix)
    log.info('cleanup done')


###############################################################################

class LiveTestBoto3Resource:

    """Base class for the Queue and Topic test wrappers.

    The method ``exists()`` must be implemented for ``generate_name()`` to
    work.
    """

    L_NAME = 1000000
    U_NAME = 10000000

    def _generate_test_name(self):
        return '%s%s' % (TEST_NAME_PREFIX,
                         str(random.randint(self.L_NAME, self.U_NAME)))

    def exists(self, name):
        """Whether the resource found by generated "name" exists or not."""
        raise NotImplementedError()

    def generate_name(self):
        """Creates a safe name to run tests.

        This method avoids the 60' delay between deleted queues.
        """
        name = self._generate_test_name()
        while self.exists(name):
            name = self._generate_test_name()
        return name

    def _is_error_call(self, response):
        """Whether the API call had an error.

        Positional parameter:
        * the `request` response object returned by the API call.
        """
        status = response.get('ResponseMetadata', {}).get('HTTPStatusCode')
        return status != 200


###############################################################################

class LiveTestQueue(LiveTestBoto3Resource):
    """
    Context manage the test SQS queue.

    Intended usage to handle setup and tear down queue:

        >>> live = LiveTestQueue()
        >>> live.create_queue()
        >>> live.queue.send_message(MessageBody='some')
        >>> msgs = live.queue.receive_messages()
        >>> print(msgs[0].body)
        >>> msg.delete()
        >>> live.destroy_queue()

    Intended usage as a context manager:

        >>> with LiveTestQueue() as queue:
        >>>   queue.send_message(MessageBody='some')
        >>>   msgs = queue.receive_messages()
        >>>   print(msgs[0].body)
        >>>   msg.delete()
    """

    def __init__(self):
        """Setup test manager.

        Assumens boto3 correctly configured.
        """
        self.queue = None
        self.queue_name = None
        self.sqs = boto3.resource('sqs')

    def exists(self, queue_name):
        for queue in self.sqs.queues.all():
            if queue_name in queue.url:
                return True
        return False

    def create_queue(self):
        """Creates a queue name and the sqs.Queue."""
        queue_name = self.generate_name()
        try:
            queue = self.sqs.create_queue(QueueName=queue_name)
        except Exception as e:
            raise RuntimeError('SQS could create queue: %s' % e)
        self.queue_name, self.queue = queue_name, queue

    def destroy_queue(self):
        """Destroy the queue (AWS SQS delays apply)."""
        response = self.queue.delete()
        if self._is_error_call(response):
            raise RuntimeError('SQS could not delete queue: %s' % response)
        self.queue, self.queue_name = None, None

    def __enter__(self):
        self.create_queue()
        return self.queue

    def __exit__(self, *args):
        self.destroy_queue()


###############################################################################

class LiveTestTopicQueue(LiveTestBoto3Resource):
    """Context manage the test SNS topics. Uses a SQS queue to receive the
    published messages.

    Intended usage to handle setup and tear down topic:

        >>> live = LiveTestTopicQueue(backend.queue)
        >>> live.create_topic_and_queue()
        >>>
        >>> live.topic.publish(Message='some')
        >>>
        >>> msgs = live.queue.receive_messages()
        >>> print(msgs[0].body)
        >>>
        >>> live.destroy_topic_and_queue()

    Intended usage as a context manager:

        >>> with LiveTestTopicQueue() as topic, queue:
        >>>     topic.publish(Message='some')
        >>>     msgs = queue.receive_messages()
        >>>     print(msgs[0].body)
    """

    def __init__(self):
        """Setup test manager.

        Assumens boto3 correctly configured.
        """
        self.topic = None
        self.topic_name = None
        self.queue = None
        self.queue_name = None
        self.queue_manager = LiveTestQueue()
        self.sns = boto3.resource('sns')

    def create_queue_policy(self, topic, queue):
        """The queue needs a policy to allow the topic to post to it."""
        return {
            'Version': '2012-10-17',
            'Statement':[
                {
                    'Sid': 'TestTopicQueuePolicy',
                    'Effect': 'Allow',
                    'Principal': '*',
                    'Action': 'sqs:SendMessage',
                    'Resource': queue.attributes['QueueArn'],
                    'Condition': {
                        'ArnEquals':{
                            'aws:SourceArn': topic.arn
                        }
                    }
                }
            ]
        }

    def replace_queue_policy(self, topic, queue):
        policy = self.create_queue_policy(topic, queue)
        queue.set_attributes(Attributes={
            'Policy': json.dumps(policy),
        })

    def exists(self, name):
        for topic in self.sns.topics.all():
            if name in topic.arn:
                return True
        return False

    def _create_topic(self):
        """Creates a topic name and the sns.Topic."""
        topic_name = self.generate_name()
        try:
            topic = self.sns.create_topic(Name=topic_name)
        except Exception as e:
            raise RuntimeError('SNS could create topic: %s' % e)
        self.topic_name, self.topic = topic_name, topic

    def _create_queue(self):
        self.queue_manager.create_queue()
        self.queue_name = self.queue_manager.queue_name
        self.queue = self.queue_manager.queue

    def create_topic_and_queue(self):
        self._create_topic()
        self._create_queue()
        self.replace_queue_policy(self.topic, self.queue)
        self.topic.subscribe(
            Protocol='sqs',
            Endpoint=self.queue.attributes['QueueArn'])

    def _destroy_topic(self):
        """Destroy the topic."""
        response = self.topic.delete()
        if self._is_error_call(response):
            raise RuntimeError('SNS could not delete topic: %s' % response)
        self.topic, self.topic_name = None, None

    def _destroy_queue(self):
        self.queue_manager.destroy_queue()
        self.queue, self.queue_name = None, None

    def destroy_topic_and_queue(self):
        self._destroy_queue()
        self._destroy_topic()

    def __enter__(self):
        self.create_topic_and_queue()
        return self.topic, self.queue_manager.queue

    def __exit__(self, *args):
        self.destroy_topic_and_queue()
