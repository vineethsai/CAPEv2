# Copyright (C) 2015 eSentire, Inc (jacob.gajek@esentire.com).
# This file is part of Cuckoo Sandbox - http://www.cuckoosandbox.org
# See the file 'docs/LICENSE' for copying permission.

from __future__ import absolute_import
import requests
import logging
import time
import random
import re

from datetime import datetime, timedelta

from lib.cuckoo.common.abstracts import Machinery
from lib.cuckoo.common.exceptions import CuckooMachineError
from lib.cuckoo.common.exceptions import CuckooDependencyError
from lib.cuckoo.common.exceptions import CuckooCriticalError
from lib.cuckoo.common.config import Config

try:
    from pyVim.connect import SmartConnection
    HAVE_PYVMOMI = True
except ImportError:
    HAVE_PYVMOMI = False

log = logging.getLogger(__name__)
logging.getLogger("requests").setLevel(logging.WARNING)
cfg = Config()

class vSphere(Machinery):
    """vSphere/ESXi machinery class based on pyVmomi Python SDK."""

    # VM states
    RUNNING = "poweredOn"
    POWEROFF = "poweredOff"
    SUSPENDED = "suspended"
    ABORTED = "aborted"

    def __init__(self):
        if not HAVE_PYVMOMI:
            raise CuckooDependencyError("Couldn't import pyVmomi. Please install "
                                        "using 'pip3 install --upgrade pyvmomi'")

        super(vSphere, self).__init__()

    def _initialize(self, module_name):
        """Read configuration.
        @param module_name: module name.
        """
        super(vSphere, self)._initialize(module_name)

        # Initialize random number generator
        random.seed()

    def _initialize_check(self):
        """Runs checks against virtualization software when a machine manager
        is initialized.
        @raise CuckooCriticalError: if a misconfiguration or unsupported state
                                    is found.
        """
        self.connect_opts = {}

        if self.options.vsphere.host:
            self.connect_opts["host"] = self.options.vsphere.host
        else:
            raise CuckooCriticalError("vSphere host address setting not found, "
                                      "please add it to the config file.")

        if self.options.vsphere.port:
            self.connect_opts["port"] = self.options.vsphere.port
        else:
            raise CuckooCriticalError("vSphere port setting not found, "
                                      "please add it to the config file.")

        if self.options.vsphere.user:
            self.connect_opts["user"] = self.options.vsphere.user
        else:
            raise CuckooCriticalError("vSphere username setting not found, "
                                      "please add it to the config file.")

        if self.options.vsphere.pwd:
            self.connect_opts["pwd"] = self.options.vsphere.pwd
        else:
            raise CuckooCriticalError("vSphere password setting not found, "
                                      "please add it to the config file.")

        # Workaround for PEP-0476 issues in recent Python versions
        if self.options.vsphere.unverified_ssl:
            import ssl
            sslContext = ssl._create_unverified_context()
            self.connect_opts["sslContext"] = sslContext
            log.warn("Turning off SSL certificate verification!")

        # Check that a snapshot is configured for each machine
        # and that it was taken in a powered-on state
        try:
            with SmartConnection(**self.connect_opts) as conn:
                for machine in self.machines():
                    if not machine.snapshot:
                        raise CuckooCriticalError("Snapshot name not specified "
                                                  "for machine {0}, please add "
                                                  "it to the config file."
                                                  .format(machine.label))
                    vm = self._get_virtual_machine_by_label(conn, machine.label)
                    if not vm:
                        raise CuckooCriticalError("Unable to find machine {0} "
                                                  "on vSphere host, please "
                                                  "update your configuration."
                                                  .format(machine.label))
                    state = self._get_snapshot_power_state(vm, machine.snapshot)
                    if not state:
                        raise CuckooCriticalError("Unable to find snapshot {0} "
                                                  "for machine {1}, please "
                                                  "update your configuration."
                                                  .format(machine.snapshot,
                                                          machine.label))
                    if state != self.RUNNING:
                        raise CuckooCriticalError("Snapshot for machine {0} not "
                                                  "in powered-on state, please "
                                                  "create one."
                                                  .format(machine.label))

        except CuckooCriticalError:
            raise

        except Exception as e:
            raise CuckooCriticalError("Couldn't connect to vSphere host")

        super(vSphere, self)._initialize_check()

    def start(self, label):
        """Start a machine.
        @param label: machine name.
        @raise CuckooMachineError: if unable to start machine.
        """
        name = self.db.view_machine_by_label(label).snapshot
        with SmartConnection(**self.connect_opts) as conn:
            vm = self._get_virtual_machine_by_label(conn, label)
            if vm:
                self._revert_snapshot(vm, name)
            else:
                raise CuckooMachineError("Machine {0} not found on host"
                                         .format(label))

    def stop(self, label):
        """Stop a machine.
        @param label: machine name.
        @raise CuckooMachineError: if unable to stop machine
        """
        with SmartConnection(**self.connect_opts) as conn:
            vm = self._get_virtual_machine_by_label(conn, label)
            if vm:
                self._stop_virtual_machine(vm)
            else:
                raise CuckooMachineError("Machine {0} not found on host"
                                         .format(label))

    def dump_memory(self, label, path):
        """Take a memory dump of a machine.
        @param path: path to where to store the memory dump
        @raise CuckooMachineError: if error taking the memory dump
        """
        name = "cuckoo_memdump_{0}".format(random.randint(100000, 999999))
        with SmartConnection(**self.connect_opts) as conn:
            vm = self._get_virtual_machine_by_label(conn, label)
            if vm:
                self._create_snapshot(vm, name)
                self._download_snapshot(conn, vm, name, path)
                self._delete_snapshot(vm, name)
            else:
                raise CuckooMachineError("Machine {0} not found on host"
                                         .format(label))

    def _list(self):
        """List virtual machines on vSphere host"""
        with SmartConnection(**self.connect_opts) as conn:
            vmlist = [vm.summary.config.name for vm in
                      self._get_virtual_machines(conn)]

        return vmlist

    def _status(self, label):
        """Get power state of vm from vSphere host.
        @param label: virtual machine name
        @raise CuckooMachineError: if error getting status or machine not found
        """
        with SmartConnection(**self.connect_opts) as conn:
            vm = self._get_virtual_machine_by_label(conn, label)
            if not vm:
                raise CuckooMachineError("Machine {0} not found on server"
                                         .format(label))

            status = vm.runtime.powerState
            self.set_status(label, status)
            return status

    def _get_virtual_machines(self, conn):
        """Iterate over all VirtualMachine managed objects on vSphere host"""
        def traverseDCFolders(conn, nodes, path=""):
            for node in nodes:
                if hasattr(node, "childEntity"):
                    for child, childpath in traverseDCFolders(conn, node.childEntity, path + node.name + "/"):
                        yield child, childpath
                else:
                    yield node, path + node.name

        def traverseVMFolders(conn, nodes):
            for node in nodes:
                if hasattr(node, "childEntity"):
                    for child in traverseVMFolders(conn, node.childEntity):
                        yield child
                else:
                    yield node

        self.VMtoDC = {}

        for dc, dcpath in traverseDCFolders(conn, conn.content.rootFolder.childEntity):
            for vm in traverseVMFolders(conn, dc.vmFolder.childEntity):
                self.VMtoDC[vm.summary.config.name] = dcpath
                yield vm

    def _get_virtual_machine_by_label(self, conn, label):
        """Return the named VirtualMachine managed object"""
        vg = (vm for vm in self._get_virtual_machines(conn)
              if vm.summary.config.name == label)
        return next(vg, None)

    def _get_snapshot_by_name(self, vm, name):
        """Return the named VirtualMachineSnapshot managed object for
           a virtual machine"""
        root = vm.snapshot.rootSnapshotList
        sg = (ss.snapshot for ss in self._traverseSnapshots(root)
              if ss.name == name)
        return next(sg, None)

    def _get_snapshot_power_state(self, vm, name):
        """Return the power state for a named VirtualMachineSnapshot object"""
        root = vm.snapshot.rootSnapshotList
        sg = (ss.state for ss in self._traverseSnapshots(root)
              if ss.name == name)
        return next(sg, None)

    def _create_snapshot(self, vm, name):
        """Create named snapshot of virtual machine"""
        log.info("Creating snapshot {0} for machine {1}"
                 .format(name, vm.summary.config.name))
        task = vm.CreateSnapshot_Task(name=name,
                                      description="Created by Cuckoo sandbox",
                                      memory=True,
                                      quiesce=False)
        try:
            self._wait_task(task)
        except CuckooMachineError as e:
            raise CuckooMachineError("CreateSnapshot: {0}".format(e))

    def _delete_snapshot(self, vm, name):
        """Remove named snapshot of virtual machine"""
        snapshot = self._get_snapshot_by_name(vm, name)
        if snapshot:
            log.info("Removing snapshot {0} for machine {1}"
                     .format(name, vm.summary.config.name))
            task = snapshot.RemoveSnapshot_Task(removeChildren=True)
            try:
                self._wait_task(task)
            except CuckooMachineError as e:
                log.error("RemoveSnapshot: {0}".format(e))
        else:
            raise CuckooMachineError("Snapshot {0} for machine {1} not found"
                                     .format(name, vm.summary.config.name))

    def _revert_snapshot(self, vm, name):
        """Revert virtual machine to named snapshot"""
        snapshot = self._get_snapshot_by_name(vm, name)
        if snapshot:
            log.info("Reverting machine {0} to snapshot {1}"
                     .format(vm.summary.config.name, name))
            task = snapshot.RevertToSnapshot_Task()
            try:
                self._wait_task(task)
            except CuckooMachineError as e:
                raise CuckooMachineError("RevertToSnapshot: {0}".format(e))
        else:
            raise CuckooMachineError("Snapshot {0} for machine {1} not found"
                                     .format(name, vm.summary.config.name))

    def _download_snapshot(self, conn, vm, name, path):
        """Download snapshot file from host to local path"""

        # Get filespec to .vmsn file of named snapshot
        snapshot = self._get_snapshot_by_name(vm, name)
        if not snapshot:
            raise CuckooMachineError("Snapshot {0} for machine {1} not found"
                                     .format(name, vm.summary.config.name))

        memorykey = datakey = filespec = None
        for s in vm.layoutEx.snapshot:
            if s.key == snapshot:
                memorykey = s.memoryKey
                datakey = s.dataKey
                break

        for f in vm.layoutEx.file:
            if f.key == memorykey and (f.type == "snapshotMemory" or
                                       f.type == "suspendMemory"):
                filespec = f.name
                break

        if not filespec:
            for f in vm.layoutEx.file:
                if f.key == datakey and f.type == "snapshotData":
                    filespec = f.name
                    break

        if not filespec:
            raise CuckooMachineError("Could not find memory snapshot file")

        log.info("Downloading memory dump {0} to {1}".format(filespec, path))

        # Parse filespec to get datastore and file path
        datastore, filepath = re.match(r"\[([^\]]*)\] (.*)", filespec).groups()

        # Construct URL request
        params = {
                "dsName" : datastore,
                "dcPath" : self.VMtoDC.get(vm.summary.config.name, "ha-datacenter") }
        headers = { "Cookie" : conn._stub.cookie }
        url = "https://{0}:{1}/folder/{2}".format(self.connect_opts["host"],
                                                  self.connect_opts["port"],
                                                  filepath)

        # Stream download to specified local path
        try:
            response = requests.get(url, params=params, headers=headers,
                                    verify=False, stream=True)

            response.raise_for_status()

            with open(path, "wb") as localfile:
                for chunk in response.iter_content(16*1024):
                    localfile.write(chunk)

        except Exception as e:
            raise CuckooMachineError("Error downloading memory dump {0}: {1}"
                                     .format(filespec, e))

    def _stop_virtual_machine(self, vm):
        """Power off a virtual machine"""
        log.info("Powering off virtual machine {0}".format(vm.summary.config.name))
        task = vm.PowerOffVM_Task()
        try:
            self._wait_task(task)
        except CuckooMachineError as e:
            log.error("PowerOffVM: {0}".format(e))

    def _wait_task(self, task):
        """Wait for a task to complete with timeout"""
        limit = timedelta(seconds=int(cfg.timeouts.vm_state))
        start = datetime.utcnow()

        while True:
            if task.info.state == "error":
                raise CuckooMachineError("Task error")

            if task.info.state == "success":
                break

            if datetime.utcnow() - start > limit:
                raise CuckooMachineError("Task timed out")

            time.sleep(1)

    def _traverseSnapshots(self, root):
        """Recursive depth-first traversal of snapshot tree"""
        for node in root:
            if len(node.childSnapshotList) > 0:
                for child in self._traverseSnapshots(node.childSnapshotList):
                    yield child
            yield node
