# Copyright (C) 2026 xsm909
#
# This file is part of xverb-plugins.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

"""libusb-1.0 through ctypes: just enough to find an MTP interface and move
bulk data.

libusb is LGPL-2.1 and is loaded, never linked: `native/` holds a build of it
for the machines it is shipped for, and one already on the machine is used
otherwise. `XVERB_MTP_LIBUSB` names another, for trying a build.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import os
import platform
import sys
from typing import List, Optional

LIBUSB_ERROR_TIMEOUT = -7


class UsbError(Exception):
    def __init__(self, what: str, code: int):
        super().__init__(f"{what}: libusb error {code}")
        self.code = code


class _DeviceDescriptor(ctypes.Structure):
    _fields_ = [
        ("bLength", ctypes.c_uint8), ("bDescriptorType", ctypes.c_uint8),
        ("bcdUSB", ctypes.c_uint16), ("bDeviceClass", ctypes.c_uint8),
        ("bDeviceSubClass", ctypes.c_uint8), ("bDeviceProtocol", ctypes.c_uint8),
        ("bMaxPacketSize0", ctypes.c_uint8), ("idVendor", ctypes.c_uint16),
        ("idProduct", ctypes.c_uint16), ("bcdDevice", ctypes.c_uint16),
        ("iManufacturer", ctypes.c_uint8), ("iProduct", ctypes.c_uint8),
        ("iSerialNumber", ctypes.c_uint8), ("bNumConfigurations", ctypes.c_uint8),
    ]


class _Endpoint(ctypes.Structure):
    _fields_ = [
        ("bLength", ctypes.c_uint8), ("bDescriptorType", ctypes.c_uint8),
        ("bEndpointAddress", ctypes.c_uint8), ("bmAttributes", ctypes.c_uint8),
        ("wMaxPacketSize", ctypes.c_uint16), ("bInterval", ctypes.c_uint8),
        ("bRefresh", ctypes.c_uint8), ("bSynchAddress", ctypes.c_uint8),
        ("extra", ctypes.c_void_p), ("extra_length", ctypes.c_int),
    ]


class _AltSetting(ctypes.Structure):
    _fields_ = [
        ("bLength", ctypes.c_uint8), ("bDescriptorType", ctypes.c_uint8),
        ("bInterfaceNumber", ctypes.c_uint8), ("bAlternateSetting", ctypes.c_uint8),
        ("bNumEndpoints", ctypes.c_uint8), ("bInterfaceClass", ctypes.c_uint8),
        ("bInterfaceSubClass", ctypes.c_uint8), ("bInterfaceProtocol", ctypes.c_uint8),
        ("iInterface", ctypes.c_uint8), ("endpoint", ctypes.POINTER(_Endpoint)),
        ("extra", ctypes.c_void_p), ("extra_length", ctypes.c_int),
    ]


class _Interface(ctypes.Structure):
    _fields_ = [("altsetting", ctypes.POINTER(_AltSetting)), ("num_altsetting", ctypes.c_int)]


class _ConfigDescriptor(ctypes.Structure):
    _fields_ = [
        ("bLength", ctypes.c_uint8), ("bDescriptorType", ctypes.c_uint8),
        ("wTotalLength", ctypes.c_uint16), ("bNumInterfaces", ctypes.c_uint8),
        ("bConfigurationValue", ctypes.c_uint8), ("iConfiguration", ctypes.c_uint8),
        ("bmAttributes", ctypes.c_uint8), ("MaxPower", ctypes.c_uint8),
        ("interface", ctypes.POINTER(_Interface)),
        ("extra", ctypes.c_void_p), ("extra_length", ctypes.c_int),
    ]


_lib = None


HERE = os.path.dirname(os.path.abspath(__file__))


def _library_paths() -> List[str]:
    """Where libusb may be, first found first: a named one, the plugin's own,
    then the system's."""
    paths = []
    given = os.environ.get("XVERB_MTP_LIBUSB")
    if given:
        paths.append(given)
    machine = platform.machine().lower()
    arch = {"x86_64": "x64", "amd64": "x64", "arm64": "arm64", "aarch64": "arm64"}.get(machine, machine)
    if sys.platform == "darwin":
        paths.append(os.path.join(HERE, "native", "macos-" + arch, "libusb-1.0.0.dylib"))
    elif sys.platform.startswith("linux"):
        paths.append(os.path.join(HERE, "native", "linux-" + arch, "libusb-1.0.so.0"))
    found = ctypes.util.find_library("usb-1.0")
    if found:
        paths.append(found)
    paths += ["/opt/homebrew/lib/libusb-1.0.0.dylib", "/usr/local/lib/libusb-1.0.0.dylib",
              "libusb-1.0.so.0"]
    return paths


def lib():
    global _lib
    if _lib is not None:
        return _lib
    l = None
    for path in _library_paths():
        if os.sep in path and not os.path.exists(path):
            continue
        try:
            l = ctypes.CDLL(path)
            break
        except OSError:
            continue
    if l is None:
        raise UsbError("libusb is not on this machine", -99)
    P, I, U8, U16, U32 = ctypes.c_void_p, ctypes.c_int, ctypes.c_uint8, ctypes.c_uint16, ctypes.c_uint
    l.libusb_init.argtypes = [ctypes.POINTER(P)]
    l.libusb_get_device_list.argtypes = [P, ctypes.POINTER(ctypes.POINTER(P))]
    l.libusb_get_device_list.restype = ctypes.c_ssize_t
    l.libusb_free_device_list.argtypes = [ctypes.POINTER(P), I]
    l.libusb_get_device_descriptor.argtypes = [P, ctypes.POINTER(_DeviceDescriptor)]
    l.libusb_get_active_config_descriptor.argtypes = [P, ctypes.POINTER(ctypes.POINTER(_ConfigDescriptor))]
    l.libusb_get_config_descriptor.argtypes = [P, U8, ctypes.POINTER(ctypes.POINTER(_ConfigDescriptor))]
    l.libusb_free_config_descriptor.argtypes = [ctypes.POINTER(_ConfigDescriptor)]
    l.libusb_ref_device.argtypes = [P]
    l.libusb_ref_device.restype = P
    l.libusb_unref_device.argtypes = [P]
    l.libusb_get_bus_number.argtypes = [P]
    l.libusb_get_bus_number.restype = U8
    l.libusb_get_device_address.argtypes = [P]
    l.libusb_get_device_address.restype = U8
    l.libusb_open.argtypes = [P, ctypes.POINTER(P)]
    l.libusb_close.argtypes = [P]
    l.libusb_claim_interface.argtypes = [P, I]
    l.libusb_release_interface.argtypes = [P, I]
    l.libusb_detach_kernel_driver.argtypes = [P, I]
    l.libusb_set_auto_detach_kernel_driver.argtypes = [P, I]
    l.libusb_clear_halt.argtypes = [P, U8]
    l.libusb_get_string_descriptor_ascii.argtypes = [P, U8, ctypes.c_char_p, I]
    l.libusb_bulk_transfer.argtypes = [P, U8, ctypes.c_void_p, I, ctypes.POINTER(I), U32]
    l.libusb_control_transfer.argtypes = [P, U8, U8, U16, U16, ctypes.c_void_p, U16, U32]
    l.libusb_strerror.argtypes = [I]
    l.libusb_strerror.restype = ctypes.c_char_p
    _lib = l
    return l


_context = None


def context():
    global _context
    if _context is None:
        ctx = ctypes.c_void_p()
        rc = lib().libusb_init(ctypes.byref(ctx))
        if rc != 0:
            raise UsbError("libusb_init", rc)
        _context = ctx
    return _context


class Candidate:
    """A device with an interface that looks like MTP, not yet opened."""

    def __init__(self, device, vendor, product, bus, address, interface,
                 ep_in, ep_out, ep_int, packet, iface_string_index, config_value,
                 serial_index=0):
        self.device = device
        self.vendor = vendor
        self.product = product
        self.bus = bus
        self.address = address
        self.interface = interface
        self.ep_in = ep_in
        self.ep_out = ep_out
        self.ep_int = ep_int
        self.packet = packet
        self.iface_string_index = iface_string_index
        self.config_value = config_value
        self.serial_index = serial_index


def _endpoints(alt: _AltSetting):
    ep_in = ep_out = ep_int = None
    packet = 512
    for k in range(alt.bNumEndpoints):
        ep = alt.endpoint[k]
        kind = ep.bmAttributes & 3
        if kind == 2:
            if ep.bEndpointAddress & 0x80:
                ep_in = ep.bEndpointAddress
                packet = ep.wMaxPacketSize or packet
            else:
                ep_out = ep.bEndpointAddress
        elif kind == 3 and ep.bEndpointAddress & 0x80:
            ep_int = ep.bEndpointAddress
    return ep_in, ep_out, ep_int, packet


def candidates() -> List[Candidate]:
    """Every interface that is still image (class 6), or vendor-specific with
    three endpoints — Android's MTP before it learned to say so. The caller
    checks the interface string for the vendor-specific ones."""
    l = lib()
    ctx = context()
    listp = ctypes.POINTER(ctypes.c_void_p)()
    n = l.libusb_get_device_list(ctx, ctypes.byref(listp))
    if n < 0:
        raise UsbError("libusb_get_device_list", n)
    found: List[Candidate] = []
    try:
        for i in range(n):
            dev = listp[i]
            desc = _DeviceDescriptor()
            if l.libusb_get_device_descriptor(dev, ctypes.byref(desc)) != 0:
                continue
            if desc.bDeviceClass == 9:  # hub
                continue
            cfg = ctypes.POINTER(_ConfigDescriptor)()
            if l.libusb_get_active_config_descriptor(dev, ctypes.byref(cfg)) != 0:
                if l.libusb_get_config_descriptor(dev, 0, ctypes.byref(cfg)) != 0:
                    continue
            try:
                c = cfg.contents
                for j in range(c.bNumInterfaces):
                    iface = c.interface[j]
                    for a in range(iface.num_altsetting):
                        alt = iface.altsetting[a]
                        ep_in, ep_out, ep_int, packet = _endpoints(alt)
                        if ep_in is None or ep_out is None:
                            continue
                        still = alt.bInterfaceClass == 6 and alt.bInterfaceSubClass == 1
                        vendor_mtp = alt.bInterfaceClass == 0xFF and alt.bNumEndpoints == 3 and ep_int is not None
                        if still or vendor_mtp:
                            found.append(Candidate(
                                l.libusb_ref_device(dev), desc.idVendor, desc.idProduct,
                                l.libusb_get_bus_number(dev), l.libusb_get_device_address(dev),
                                alt.bInterfaceNumber, ep_in, ep_out, ep_int, packet,
                                alt.iInterface if not still else 0, c.bConfigurationValue,
                                desc.iSerialNumber))
            finally:
                l.libusb_free_config_descriptor(cfg)
    finally:
        l.libusb_free_device_list(listp, 1)
    return found


class Handle:
    def __init__(self, cand: Candidate):
        l = lib()
        self.cand = cand
        h = ctypes.c_void_p()
        rc = l.libusb_open(cand.device, ctypes.byref(h))
        if rc != 0:
            raise UsbError("open", rc)
        self.h = h
        self.claimed = False

    def string(self, index: int) -> str:
        if not index:
            return ""
        buf = ctypes.create_string_buffer(256)
        n = lib().libusb_get_string_descriptor_ascii(self.h, index, buf, 256)
        return buf.raw[:n].decode("ascii", "replace") if n > 0 else ""

    def claim(self):
        l = lib()
        l.libusb_set_auto_detach_kernel_driver(self.h, 1)
        rc = l.libusb_claim_interface(self.h, self.cand.interface)
        if rc != 0:
            raise UsbError("claim interface", rc)
        self.claimed = True

    def write(self, data: bytes, timeout_ms: int = 10000) -> None:
        l = lib()
        done = ctypes.c_int()
        view = memoryview(data)
        buf = (ctypes.c_char * len(data)).from_buffer_copy(data) if data else None
        rc = l.libusb_bulk_transfer(self.h, self.cand.ep_out, buf, len(data), ctypes.byref(done), timeout_ms)
        if rc != 0:
            raise UsbError("bulk out", rc)
        if done.value != len(view):
            raise UsbError(f"bulk out short ({done.value}/{len(view)})", -99)

    def read(self, size: int, timeout_ms: int = 10000) -> bytes:
        l = lib()
        buf = ctypes.create_string_buffer(size)
        done = ctypes.c_int()
        rc = l.libusb_bulk_transfer(self.h, self.cand.ep_in, buf, size, ctypes.byref(done), timeout_ms)
        if rc != 0:
            raise UsbError("bulk in", rc)
        return buf.raw[:done.value]

    def clear_halt(self):
        l = lib()
        l.libusb_clear_halt(self.h, self.cand.ep_in)
        l.libusb_clear_halt(self.h, self.cand.ep_out)

    def close(self):
        l = lib()
        if self.claimed:
            l.libusb_release_interface(self.h, self.cand.interface)
            self.claimed = False
        if self.h:
            l.libusb_close(self.h)
            self.h = None
