# -*- coding: utf-8 -*-
# AndroidDevice without uiautomator

import os
import re
import cv2
import time
import Queue
import struct
import socket
import warnings
import traceback
import threading
import subprocess
import collections
import numpy as np

from atx import patch
from atx.adb import Adb
from atx.device.device_mixin import DeviceMixin

DISPLAY_RE = re.compile(
    r'.*DisplayViewport{valid=true, .*orientation=(?P<orientation>\d+), .*deviceWidth=(?P<width>\d+), deviceHeight=(?P<height>\d+).*')
PROP_PATTERN = re.compile(
    r'\[(?P<key>.*?)\]:\s*\[(?P<value>.*)\]')

class SubAdb(Adb):
    def __init__(self, *args, **kwargs):
        super(SubAdb, self).__init__(*args, **kwargs)
        self.subs = {}

    def start_daemon(self, name, cmds, listener=None):
        if name in self.subs:
            p = self.subs.pop(name)
            p.kill()

        if listener is None:
            self.subs[name] = subprocess.Popen(cmds)
            return

        queue = Queue.Queue()
        p = subprocess.Popen(cmds, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        self.subs[name] = p
        
        # pull data from pipe, readline will block in subprocess
        def pull():
            while True:
                line = p.stdout.readline().strip()
                if not line:
                    if p.poll() is not None:
                        break
                queue.put(line)
            p.stdout.close()

        t = threading.Thread(target=pull)
        t.setDaemon(True)
        t.start()

        # listen without block
        def listen():
            while True:
                try:
                    time.sleep(0.005)
                    line = queue.get_nowait()
                    listener(line)
                except Queue.Empty():
                    if p.poll() is not None:
                        break
                    continue
                except:
                    pass

        t = threading.Thread(target=listen)
        t.setDaemon(True)
        t.start()

    def start_minicap_daemon(self, params, name='minicap', port=1313, listener=None):
        if name in self.subs:
            p = self.subs.pop(name)
            p.kill()
            print 'stop p', p.pid

        cmds = 'adb shell LD_LIBRARY_PATH=/data/local/tmp /data/local/tmp/minicap -P %s -S' % params
        p = subprocess.Popen(cmds, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        self.subs[name] = p
        print 'start new p', p.pid
        # wait for minicap server to start
        time.sleep(3)
        subprocess.call('adb forward tcp:%s localabstract:minicap' % port)

        queue = Queue.Queue()
        # pull data from socket
        def pull():
            print 'start pull', p.pid, p.poll()
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.connect(('127.0.0.1', port))
            try:
                t = s.recv(24)
                print 'minicap conntected', struct.unpack('<2B5I2B', t)
                while True:
                    frame_size = struct.unpack("<I", s.recv(4))[0]
                    trunks = []
                    recvd_size = 0
                    while recvd_size < frame_size:
                        trunk_size = min(8192, frame_size-recvd_size)
                        d = s.recv(trunk_size)
                        trunks.append(d)
                        recvd_size += len(d)
                    queue.put(''.join(trunks))
            except Exception as e:
                if not isinstance(e, struct.error):
                    traceback.print_exc()
                p.kill()
                subprocess.call('adb forward --remove tcp:%s' % port)
            finally:
                s.close()

        t = threading.Thread(target=pull)
        t.setDaemon(True)
        t.start()

        def listen():
            while True:
                try:
                    time.sleep(0.005)
                    line = queue.get_nowait()
                    listener(line)
                except Queue.Empty:
                    if p.poll() is not None:
                        break
                    continue
                except:
                    traceback.print_exc()

        t = threading.Thread(target=listen)
        t.setDaemon(True)
        t.start()


    def check_output(self, *args):
        cmds = self._assemble(*args)
        output = subprocess.check_output(cmds, stderr=subprocess.STDOUT)
        return output.replace('\r\n', '\n')

    def __call__(self, *args):
        '''
        Run adb command, for example: adb(['pull', '/data/local/tmp/a.png'])

        Args:
            command: string or list of string

        Returns:
            command output
        '''
        cmds = self._assemble(*args)
        subprocess.call(cmds)

    def _assemble(self, *args):
        cmds = ['adb']
        serial = self.device_serial()
        if serial:
            cmds.extend(['-s', serial])
        cmds.extend(self.adb_host_port_options)
        cmds.extend(list(args))
        # print cmds
        return cmds

def getenv(name, default_value=None, type=str):
    value = os.getenv(name)
    return type(value) if value else default_value

class AndroidDeviceMinicap(DeviceMixin):
    def __init__(self, *args, **kwargs):
        serialno = kwargs.get('serialno', getenv('ATX_ADB_SERIALNO', None))
        self._host = kwargs.get('host', getenv('ATX_ADB_HOST', '127.0.0.1'))
        self._port = kwargs.get('port', getenv('ATX_ADB_PORT', 5037, type=int))
        self._adb = SubAdb(serialno, self._host, self._port)
        serialno = self._adb.device_serial()
        self._serial = serialno
        
        super(AndroidDeviceMinicap, self).__init__()

        self.screen_rotation = 0
        w, h = self.display
        self._screen = np.ndarray((h,w,3), dtype=np.uint8)
        self._watch_orientation()
        self.last_screenshot = None

    def _watch_screen(self):
        params = self._minicap_params()
        print 'watch screen', params
        self._adb.start_minicap_daemon(params, listener=self._update_screen)

    def _watch_orientation(self):
        out = subprocess.check_output('adb shell pm path jp.co.cyberagent.stf.rotationwatcher')
        path = out.strip().split(':')[-1]
        cmd = 'adb shell CLASSPATH="%s" app_process /system/bin "jp.co.cyberagent.stf.rotationwatcher.RotationWatcher"' % path
        self._adb.start_daemon('orientation', cmd, listener=self._update_orientation)

    def _update_orientation(self, value):
        print 'update orientation to', value
        self.screen_rotation = int(value)/90
        self._watch_screen()

    def _minicap_params(self):
        rotation = self.screen_rotation
        return '{x}x{y}@{x}x{y}/{r}'.format(
            x=self.display.width,
            y=self.display.height,
            r=rotation*90)

    def _update_screen(self, frame):
        def str2img(jpgstr):
            arr = np.fromstring(jpgstr, np.uint8)
            img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
            print img.shape
            return img
        self._screen = str2img(frame)

    def screenshot_cv2(self):
        return self._screen.copy()

    @property
    def wlan_ip(self):
        """ Wlan IP """
        return self.adb_shell(['getprop', 'dhcp.wlan0.ipaddress']).strip()

    def forward(self, device_port, local_port=None):
        """Forward device port to local
        Args:
            device_port: port inside device
            local_port: port on PC, if this value is None, a port will random pick one.

        Returns:
            tuple, (host, local_port)
        """
        port = self._adb.forward(device_port, local_port)
        return (self._host, port)

    @property
    def current_package_name(self):
        return self.info['currentPackageName']

    def is_app_alive(self, package_name):
        """ Deprecated: use current_package_name instaed.
        Check if app in running in foreground """
        return self.info['currentPackageName'] == package_name

    def sleep(self, secs=None):
        """Depreciated. use delay instead."""
        self.delay(secs)

    @property
    @patch.run_once
    def display(self):
        """Virtual keyborad may get small d.info['displayHeight']
        """
        w, h = (0, 0)
        for line in self.adb_shell('dumpsys display').splitlines():
            m = DISPLAY_RE.search(line, 0)
            if not m:
                continue
            w = int(m.group('width'))
            h = int(m.group('height'))
            # o = int(m.group('orientation'))
            w, h = min(w, h), max(w, h)
            return collections.namedtuple('Display', ['width', 'height'])(w, h)

        w, h = self.info['displayWidth'], self.info['displayHeight']
        w, h = min(w, h), max(w, h)
        return collections.namedtuple('Display', ['width', 'height'])(w, h)

    @property
    def rotation(self):
        """
        Rotaion of the phone

        0: normal
        1: home key on the right
        2: home key on the top
        3: home key on the left
        """
        if self.screen_rotation in range(4):
            return self.screen_rotation
        return self.info['displayRotation']

    def adb_cmd(self, command):
        '''
        Run adb command, for example: adb(['pull', '/data/local/tmp/a.png'])

        Args:
            command: string or list of string

        Returns:
            command output
        '''
        cmds = ['adb']
        if self._serial:
            cmds.extend(['-s', self._serial])
        cmds.extend(['-H', self._host, '-P', str(self._port)])

        if isinstance(command, list) or isinstance(command, tuple):
            cmds.extend(list(command))
        else:
            cmds.append(command)
        # print cmds
        output = subprocess.check_output(cmds, stderr=subprocess.STDOUT)
        return output.replace('\r\n', '\n')

    def adb_shell(self, command):
        '''
        Run adb shell command

        Args:
            command: string or list of string

        Returns:
            command output
        '''
        if isinstance(command, list) or isinstance(command, tuple):
            return self.adb_cmd(['shell'] + list(command))
        else:
            return self.adb_cmd(['shell'] + [command])

    @property
    def properties(self):
        '''
        Android Properties, extracted from `adb shell getprop`

        Returns:
            dict of props, for
            example:

                {'ro.bluetooth.dun': 'true'}
        '''
        props = {}
        for line in self.adb_shell(['getprop']).splitlines():
            m = PROP_PATTERN.match(line)
            if m:
                props[m.group('key')] = m.group('value')
        return props

    def start_app(self, package_name):
        '''
        Start application

        Args:
            package_name: string like com.example.app1

        Returns:
            None
        '''
        self.adb_shell(['monkey', '-p', package_name, '-c', 'android.intent.category.LAUNCHER', '1'])
        return self

    def stop_app(self, package_name, clear=False):
        '''
        Stop application

        Args:
            package_name: string like com.example.app1
            clear: bool, remove user data

        Returns:
            None
        '''
        if clear:
            self.adb_shell(['pm', 'clear', package_name])
        else:
            self.adb_shell(['am', 'force-stop', package_name])
        return self

    def takeSnapshot(self, filename):
        '''
        Deprecated, use screenshot instead.
        '''
        warnings.warn("deprecated, use snapshot instead", DeprecationWarning)
        return self.screenshot(filename)

    def type(self, text):
        """Input some text, TODO(ssx): not tested.
        Args:
            text: string (text to input)
        """
        self.adb_shell(['input', 'text', text])
        return self