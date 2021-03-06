"""
Copyright 2018 Splunk, Inc..

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

      http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
"""

import json
import logging
import time
import LogEntry_pb2
import subprocess
import struct
import requests
import os
import sys
from multiprocessing import Pool
import requests_unixsocket
from requests.packages.urllib3.util.retry import Retry
from requests.adapters import HTTPAdapter


TIMEROUT = 500
SOCKET_START_URL = "http+unix://%2Frun%2Fdocker%2Fplugins%2Fsplunklog.sock/" \
                    "LogDriver.StartLogging"
SOCKET_STOP_URL = "http+unix://%2Frun%2Fdocker%2Fplugins%2Fsplunklog.sock/" \
                  "LogDriver.StopLogging"


logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
formatter = logging.Formatter('%(asctime)s - %(name)s -' +
                              ' %(levelname)s - %(message)s')
handler = logging.StreamHandler(sys.stdout)
handler.setFormatter(formatter)
logger.addHandler(handler)


def start_logging_plugin(plugin_path):
    '''Start to run logging plugin binary'''
    args = (plugin_path)
    popen = subprocess.Popen(args, stdout=subprocess.PIPE)

    return popen


def kill_logging_plugin(plugin_path):
    '''Kill running logging plugin process'''
    os.system("killall " + plugin_path)


def start_log_producer_from_input(file_path, test_input, u_id, timeout=0):
    '''
    Spawn a thread to write logs to fifo from the given test input
    @param: file_path
    @param: test_input
            type: array of tuples of (string, Boolean)
    @param: u_id
    @param: timeout
    '''
    pool = Pool(processes=1)              # Start a worker processes.
    pool.apply_async(__write_to_fifo, [file_path, test_input, u_id, timeout])


def start_log_producer_from_file(file_path, u_id, input_file):
    '''
    Spawn a thread to write logs to fifo by streaming the
    content from given file
    @param: file_path
    @param: u_id
    @param: input_file
    '''
    pool = Pool(processes=1)              # Start a worker processes.
    pool.apply_async(__write_file_to_fifo, [file_path, u_id, input_file])


def __write_to_fifo(fifo_path, test_input, u_id, timeout=0):
    f_writer = __open_fifo(fifo_path)

    for message, partial in test_input:
        logger.info("Writing data in protobuf with source=%s", u_id)
        if timeout != 0:
            time.sleep(timeout)
        __write_proto_buf_message(f_writer,
                                  message=message,
                                  partial=partial,
                                  source=u_id)

    __close_fifo(f_writer)


def __write_file_to_fifo(fifo_path, u_id, input_file):
    f_writer = __open_fifo(fifo_path)

    logger.info("Writing data in protobuf with source=%s", u_id)

    message = ""
    with open(input_file, "r") as f:
        message = f.read(15360)  # 15kb
        while not message.endswith("\n"):
            __write_proto_buf_message(f_writer,
                                      message=message,
                                      partial=True,
                                      source=u_id)
            message = f.read(15360)
        # write the remaining
        __write_proto_buf_message(f_writer,
                                  message=message,
                                  partial=False,
                                  source=u_id)

    f.close()
    __close_fifo(f_writer)


def __open_fifo(fifo_location):
    '''create and open a file'''
    if os.path.exists(fifo_location):
        os.unlink(fifo_location)

    os.mkfifo(fifo_location)
    fifo_writer = open(fifo_location, 'wb')

    return fifo_writer


def __write_proto_buf_message(fifo_writer=None,
                              source="test",
                              time_nano=int(time.time() * 1000000000),
                              message="",
                              partial=False,
                              id=""):
    '''
    write to fifo in the format of LogMessage protobuf
    '''
    log = LogEntry_pb2.LogEntry()
    log.source = source
    log.time_nano = time_nano
    log.line = bytes(message, "utf8")
    log.partial = partial

    buf = log.SerializeToString(log)
    size = len(buf)

    size_buffer = bytearray(4)
    struct.pack_into(">i", size_buffer, 0, size)
    fifo_writer.write(size_buffer)
    fifo_writer.write(buf)
    fifo_writer.flush()


def __close_fifo(fifo_writer):
    '''close a file'''
    # os.close(fifo_writer)
    fifo_writer.close()


def request_start_logging(file_path, hec_url, hec_token, options={}):
    '''
    send a request to the plugin to start logging
    :param file_path: the file path
    :type file_path: string

    :param hec_url: the file path
    :type hec_url: string

    :param hec_token: the file path
    :type hec_token: string
    '''
    config = {}
    config["splunk-url"] = hec_url
    config["splunk-token"] = hec_token
    config["splunk-insecureskipverify"] = "true"
    config["splunk-format"] = "json"
    config["tag"] = ""

    config = {**config, **options}

    req_obj = {
        "File": file_path,
        "Info": {
            "ContainerID": "test",
            "Config": config,
            "LogPath": "/home/ec2-user/test.txt"
        }
    }

    headers = {
        "Content-Type": "application/json",
        "Host": "localhost"
    }

    session = requests_unixsocket.Session()
    res = session.post(
        SOCKET_START_URL,
        data=json.dumps(req_obj),
        headers=headers)

    if res.status_code != 200:
        raise Exception("Can't establish socket connection")

    logger.info(res.json())


def request_stop_logging(file_path):
    '''
    send a request to the plugin to stop logging
    '''
    req_obj = {
        "File": file_path
    }
    session = requests_unixsocket.Session()
    res = session.post(
        SOCKET_STOP_URL,
        data=json.dumps(req_obj)
    )

    if res.status_code != 200:
        raise Exception("Can't establish socket connection")

    logger.info(res.json())


def check_events_from_splunk(index="main",
                             id=None,
                             start_time="-24h@h",
                             end_time="now",
                             url="",
                             user="",
                             password=""):
    '''
    send a search request to splunk and return the events from the result
    '''
    query = _compose_search_query(index, id)
    events = _collect_events(query, start_time, end_time, url, user, password)

    return events


def _compose_search_query(index="main", id=None):
    return "search index={0} {1}".format(index, id)


def _collect_events(query, start_time, end_time, url="", user="", password=""):
    '''
    Collect events by running the given search query
    @param: query (search query)
    @param: start_time (search start time)
    @param: end_time (search end time)
    returns events
    '''

    search_url = '{0}/services/search/jobs?output_mode=json'.format(
        url)
    logger.info('requesting: %s', search_url)
    data = {
        'search': query,
        'earliest_time': start_time,
        'latest_time': end_time,
    }

    create_job = _requests_retry_session().post(
        search_url,
        auth=(user, password),
        verify=False, data=data)
    _check_request_status(create_job)

    json_res = create_job.json()
    job_id = json_res['sid']
    events = _wait_for_job_and__get_events(job_id, url, user, password)

    return events


def _wait_for_job_and__get_events(job_id, url="", user="", password=""):
    '''
    Wait for the search job to finish and collect the result events
    @param: job_id
    returns events
    '''
    events = []
    job_url = '{0}/services/search/jobs/{1}?output_mode=json'.format(
        url, str(job_id))
    logger.info('requesting: %s', job_url)

    for _ in range(TIMEROUT):
        res = _requests_retry_session().get(
            job_url,
            auth=(user, password),
            verify=False)
        _check_request_status(res)

        job_res = res.json()
        dispatch_state = job_res['entry'][0]['content']['dispatchState']

        if dispatch_state == 'DONE':
            events = _get_events(job_id, url, user, password)
            break
        if dispatch_state == 'FAILED':
            raise Exception('Search job: {0} failed'.format(job_url))
        time.sleep(1)

    return events


def _get_events(job_id, url="", user="", password=""):
    '''
    collect the result events from a search job
    @param: job_id
    returns events
    '''
    event_url = '{0}/services/search/jobs/{1}/events?output_mode=json'.format(
        url, str(job_id))
    logger.info('requesting: %s', event_url)

    event_job = _requests_retry_session().get(
        event_url, auth=(user, password),
        verify=False)
    _check_request_status(event_job)

    event_job_json = event_job.json()
    events = event_job_json['results']

    return events


def _check_request_status(req_obj):
    '''
    check if a request is successful
    @param: req_obj
    returns True/False
    '''
    if not req_obj.ok:
        raise Exception('status code: {0} \n details: {1}'.format(
            str(req_obj.status_code), req_obj.text))


def _requests_retry_session(
        retries=10,
        backoff_factor=0.1,
        status_forcelist=(500, 502, 504)):
    '''
    create a retry session for HTTP/HTTPS requests
    @param: retries (num of retry time)
    @param: backoff_factor
    @param: status_forcelist (list of error status code to trigger retry)
    @param: session
    returns: session
    '''
    session = requests.Session()
    retry = Retry(
        total=int(retries),
        backoff_factor=backoff_factor,
        method_whitelist=frozenset(['GET', 'POST']),
        status_forcelist=status_forcelist,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount('http://', adapter)
    session.mount('https://', adapter)

    return session
