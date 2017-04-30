import hashlib
import heapq
import io
import json
import logging
import os
import pickle
import sys
import uuid
import urllib.request
import socket
from subprocess import Popen, PIPE
from multiprocessing.pool import ThreadPool

from tornado import gen, process, netutil, ioloop, httpserver
from tornado.httpclient import AsyncHTTPClient
from tornado.web import RequestHandler, Application

# sys.path.insert(1, os.path.join(sys.path[0], '..'))
from code import inventory
sys.setrecursionlimit(10000)

inventory.init_ports()
root = os.path.dirname(__file__)

log = logging.getLogger(__name__)

global_map_dict = {}

# timeout in seconds
timeout = 10000
socket.setdefaulttimeout(timeout)

worker_servers = [inventory.HOSTNAME + ":" + str(p) for p in inventory.WORKER_PORTS]


class MapHandler(RequestHandler):
    @gen.coroutine
    def get(self):
        print("INSIDE MAP HANDLER")
        mapper_path = self.get_argument('mapper_path')
        input_file = self.get_argument('input_file')
        num_reducers = int(self.get_argument('num_reducers'))
        p = Popen(['python', mapper_path], stdin=PIPE, stdout=PIPE, stderr=PIPE)
        output, err = p.communicate(open(input_file, "rb").read())
        rc = p.returncode
        if rc == 0:
            print("RETURN CODE ZERO, PROCESSING")
            map_task_id = str(uuid.uuid4())
            global_map_dict[map_task_id] = {}
            for reducer_idx in range(num_reducers):
                global_map_dict[map_task_id][reducer_idx] = []
            output = output.decode('utf-8', errors="strict")
            for line in output.split('\n'):
                try:
                    key, val = line.split('\t')
                    reducer_idx = int(key.split('.')[0]) % num_reducers
                    global_map_dict[map_task_id][reducer_idx].append((key, val))

                except ValueError:
                    continue
            for reducer_idx in global_map_dict[map_task_id]:
                global_map_dict[map_task_id][reducer_idx].sort()
            self.write(json.dumps({"status": "success", "map_task_id": map_task_id}))
        else:
            self.write(json.dumps({"status": "failure", "map_task_id": 0}))
        self.finish()


class RetrieveMapOutputHandler(RequestHandler):
    @gen.coroutine
    def get(self):

        found = False
        reducer_ix = int(self.get_argument('reducer_ix'))
        map_task_id = self.get_argument('map_task_id')

        if map_task_id in global_map_dict:
            if reducer_ix in global_map_dict[map_task_id]:
                self.write(json.dumps(global_map_dict[map_task_id][reducer_ix]))
                found = True

        if not found:
            self.write(json.dumps({}))

        self.finish()


class ReduceHandler(RequestHandler):
    @gen.coroutine
    def get(self):
        reducer_ix = self.get_argument('reducer_ix')
        reducer_path = self.get_argument('reducer_path')
        map_task_ids = self.get_argument('map_task_ids').split(',')
        job_path = self.get_argument('job_path')
        http_client = AsyncHTTPClient()
        http_client.configure(None, defaults=dict(connect_timeout=2000000, request_timeout=80000000, max_clients=100000000))
        results_to_sort = []
        futures = []
        urls =[]
        responses = []
        count = 0
        print("INSIDE REDUCE HANDLER")
        for i, map_task_id in enumerate(map_task_ids):
            server = worker_servers[i % inventory.WORKER_THREAD_COUNT]
            count += 1
            url = server + "/retrieve_map_output?reducer_ix=" + str(reducer_ix) + "&map_task_id=" + map_task_id
            urls.append(url)
            # futures.append(http_client.fetch(url))
            # responses.append(urllib.request.urlopen(url, timeout=100000))

            # res = yield http_client.fetch(url)

        responses =  yield self.thread_helper(urls)
        print("RESPONSES RECEIVED")
        for res in responses:
            # result = json.loads(res.body.decode('utf-8'))
            html = res.read()
            encoding = res.info().get_content_charset('utf-8')
            result = json.loads(html.decode(encoding))
            if len(result) > 0:
                result = [tuple(l) for l in result]
                results_to_sort.append(result)
        merged = heapq.merge(*results_to_sort)

        p = Popen(['python', reducer_path], stdin=PIPE, stdout=PIPE, stderr=PIPE)
        sio = io.StringIO()
        for tup in merged:
            to_write = str(tup[0]) + '\t' + str(tup[1]) + '\n'
            sio.write(to_write)
        message = sio.getvalue().encode('utf-8')
        output, err = p.communicate(message)
        rc = p.returncode
        print("Return code: {}".format(rc))
        if rc == 0:
            target = open(job_path + "/" + reducer_ix + ".out", 'wb')
            pickle.dump(pickle.loads(output), target)

            # for line in output:
            #     target.write(line)
            #     target.write('\n')

            target.close()
        else:
            print(output, err)
        self.write(json.dumps({"status": "success"}))

    @gen.coroutine
    def thread_helper(self, urls):
        pool = ThreadPool(inventory.WORKER_THREAD_COUNT)
        results = pool.map(urllib.request.urlopen, urls)
        pool.close()
        pool.join()
        return results


def create_workers(port):
    app = Application(handlers=[
        (r"/map", MapHandler),
        (r"/retrieve_map_output", RetrieveMapOutputHandler),
        (r"/reduce", ReduceHandler)
    ])

    app.listen(port)


def start_workers():
    # num_procs = inventory.WORKER_THREAD_COUNT
    # task_id = process.fork_processes(num_procs, max_restarts=0)
    port = inventory.WORKER_PORTS
    # app = httpserver.HTTPServer(create_workers())
    log.info("Worker is listening on %s", inventory.HOSTNAME + ":" + str(port))
    for p in port:
        try:
            create_workers(p)
        except OSError:
            print(p, " crying")


if __name__ == '__main__':
    logging.basicConfig(format='%(levelname)s - %(asctime)s - %(message)s', level=logging.DEBUG)
    start_workers()
    try:
        ioloop.IOLoop.instance().start()
    except KeyboardInterrupt:
        log.info("Shutting down services")
        ioloop.IOLoop.instance().stop()
