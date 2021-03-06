from apscheduler.executors.gevent import GeventExecutor
from requests import get
from .system import free_memory
from gevent import sleep
from logging import getLogger
from random import random
import consul
import iso8601
from datetime import timedelta, datetime
from apscheduler.schedulers.gevent import GeventScheduler
from gevent.subprocess import check_call, CalledProcessError, Popen

from uuid import uuid4

LOCK_RETRIES = 6
SLEEP_BETWEEN_TRIES_LOCK = 10
WORKER_TIME_RUN = 16 * 60

AWS_META_DATA_URL = 'http://169.254.169.254/latest/meta-data/instance-id'
SERVER_NAME_PREFIX = 'AUCTION_WORKER_{}'

MIN_AUCTION_START_TIME_RESERV = timedelta(seconds=60)
MAX_AUCTION_START_TIME_RESERV = timedelta(seconds=15 * 60)

def get_server_name():
    try:
        r = get(AWS_META_DATA_URL, timeout=10)
        suffix = r.body_string()
    except Exception, e:
        suffix = uuid4().hex
    return SERVER_NAME_PREFIX.format(suffix)



class AuctionExecutor(GeventExecutor):

    def start(self, scheduler, alias):
        return super(AuctionExecutor, self).start(scheduler, alias)

    def shutdown(self, wait=True):
        """
        Shuts down this executor.

        :param bool wait: ``True`` to wait until all submitted jobs
            have been executed
        """
        while len(self._instances) > 0:
            sleep(1)

    def _run_job_success(self, job_id, events):
        """Called by the executor with the list of generated events when `run_job` has been successfully called."""
        super(GeventExecutor, self)._run_job_success(job_id, events)
        self.cleanup_jobs_instances(job_id)

    def _run_job_error(self, job_id, exc, traceback=None):
        """Called by the executor with the exception if there is an error calling `run_job`."""
        super(GeventExecutor, self)._run_job_error(job_id, exc, traceback=traceback)
        self.cleanup_jobs_instances(job_id)

    def cleanup_jobs_instances(self, job_id):
        with self._lock:
            if self._instances[job_id] == 0:
                del self._instances[job_id]

class AuctionScheduler(GeventScheduler):
    def __init__(self, server_name, config,
                 limit_auctions=500,
                 limit_free_memory=0.15,
                 logger=getLogger(__name__),
                 *args, **kwargs):
        super(AuctionScheduler, self).__init__(*args, **kwargs)
        self.server_name = server_name
        self.config = config
        self.execution_stopped = False
        self.use_consul = self.config.get('main', {}).get('use_consul', True)
        if self.use_consul:
            self.consul = consul.Consul()
        self.logger = logger
        self._limit_pool_lock = self._create_lock()
        self._limit_auctions = self.config['main'].get('limit_auctions', int(limit_auctions))
        self._limit_free_memory = self.config['main'].get('limit_free_memory', float(limit_free_memory))
        self._count_auctions = 0
        self.exit = False
        self.processes = {}

    def _create_default_executor(self):
        return AuctionExecutor()

    def convert_datetime(self, datetime_stamp):
        return iso8601.parse_date(datetime_stamp).astimezone(self.timezone)

    def get_auction_worker_configuration_path(self, view_value, key='api_version'):
        value = view_value.get(key, '')
        if value:
            return self.config['main'].get(
                'auction_worker_config_for_{}_{}'.format(key, value), self.config['main']['auction_worker_config']
            )

        return self.config['main']['auction_worker_config']

    def shutdown(self, SIGKILL=False):
        self.exit = True
        if SIGKILL:
            for pid in self.processes:
                self.logger.info("Killed {}".format(pid))
                self.processes[pid].terminate()
        response = super(AuctionScheduler, self).shutdown()
        self.execution_stopped = True
        return response

    def _auction_fucn(self, document_id, tender_id, lot_id, view_value):
        process = None
        params = [self.config['main']['auction_worker'],
                  "run", tender_id,
                  self.get_auction_worker_configuration_path(view_value)]
        if lot_id:
            params += ['--lot', lot_id]

        if view_value['api_version']:
            params += ['--with_api_version', view_value['api_version']]

        if view_value['mode'] == 'test':
            params += ['--auction_info_from_db', 'true']

        try:
            process = Popen(params)
            self.processes[process.pid] = process
            rc = process.wait()
            if rc == 0:
                self.logger.info(
                    "Finished {}".format(document_id),
                    extra={'MESSAGE_ID': 'CHRONOGRAPH_WORKER_COMPLETE_SUCCESSFUL'})
            else:
                self.logger.error(
                    "Exit with error {}".format(document_id),
                    extra={'MESSAGE_ID': 'CHRONOGRAPH_WORKER_COMPLETE_EXCEPTION'})
        except Exception, error:
            self.logger.critical(
                "Exit with error {} params: {} error: {}".format(document_id, repr(params), repr(error)),
                extra={'MESSAGE_ID': 'CHRONOGRAPH_WORKER_COMPLETE_EXCEPTION'})
        if process:
            del self.processes[process.pid]

    def run_auction_func(self, tender_id, lot_id, view_value, ttl=WORKER_TIME_RUN):
        if self._count_auctions >= self._limit_auctions:
            self.logger.info("Limited by count")
            return

        if free_memory() <= self._limit_free_memory:
            self.logger.info("Limited by memory")
            return

        document_id = str(tender_id)
        if lot_id:
            document_id += "_"
            document_id += lot_id

        sleep(random())
        if self.use_consul:
            i = LOCK_RETRIES
            session = self.consul.session.create(behavior='delete', ttl=WORKER_TIME_RUN)
            while i > 0:
                if self.consul.kv.put("auction_{}".format(document_id), self.server_name, acquire=session):
                    self.logger.info("Run worker for document {}".format(document_id),
                                     extra={'MESSAGE_ID': 'CHRONOGRAPH_RUN_WORKER'})
                    with self._limit_pool_lock:
                        self._count_auctions += 1

                    self._auction_fucn(document_id, tender_id, lot_id,
                                       view_value)

                    self.logger.info("Finished {}".format(document_id))
                    self.consul.session.destroy(session)
                    with self._limit_pool_lock:
                        self._count_auctions -= 1
                    return
                sleep(SLEEP_BETWEEN_TRIES_LOCK)
                i -= 1

            self.logger.debug("Locked on other server")
            self.consul.session.destroy(session)
        else:
            self.logger.info("Run worker for document {}".format(document_id),
                             extra={'MESSAGE_ID': 'CHRONOGRAPH_RUN_WORKER'})
            self._auction_fucn(document_id, tender_id, lot_id, view_value)

    def schedule_auction(self, document_id, view_value):
        auction_start_date = self.convert_datetime(view_value['start'])
        if self._executors['default']._instances.get(document_id):
            return
        job = self.get_job(document_id)
        if job:
            job_auction_start_date = job.args[2]['start'] # job.args[2] view_value
            if job_auction_start_date == auction_start_date:
                return
            self.logger.warning("Changed start date: {}".format(document_id))

        now = datetime.now(self.timezone)
        if auction_start_date - now > MAX_AUCTION_START_TIME_RESERV:
            AW_date = auction_start_date - MAX_AUCTION_START_TIME_RESERV
        elif auction_start_date - now > MIN_AUCTION_START_TIME_RESERV:
            self.logger.warning('Planned auction\'s starts date in the past')
            AW_date = now
        else:
            return

        if "_" in document_id:
            tender_id, lot_id = document_id.split("_")
        else:
            tender_id = document_id
            lot_id = None
        self.logger.info('Scedule start of {} at {} ({})'.format(document_id, AW_date, view_value['start']),
                         extra={'MESSAGE_ID': 'CHRONOGRAPH_PLANNED_WORKER'})

        self.add_job(self.run_auction_func, args=(tender_id, lot_id, view_value),
                          misfire_grace_time=60,
                          next_run_time=AW_date,
                          id=document_id,
                          replace_existing=True)
