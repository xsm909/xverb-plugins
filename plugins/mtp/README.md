# Android phones

An Android phone on a USB cable, in a panel like the local disk: browse it,
view what is on it, copy both ways, make folders, rename, move and delete.
Since Google stopped maintaining Android File Transfer, macOS has had no way
of its own to do this. Finder does not speak MTP.

## Connecting

Plug the phone in, unlock it, and set USB to **File transfer**: the
notification that says the phone is charging over USB opens that choice. If
the phone asks whether to allow access to its data, allow it. The phone then
appears under **Drives and connections** by itself, one row per storage, with
the space free. A locked phone shows no storage at all, and its row says so.

A URL is `mtp://SERIAL/STORAGE/path`: the phone's USB serial number, then the
storage by the name the phone gives it, then the path.

## How it works

Three layers, one file each:

| | |
| --- | --- |
| `usb.py` | libusb through `ctypes` — find the MTP interface, move bulk data |
| `ptp.py` | MTP: sessions, storages, objects, reading, sending |
| `main.py` | the adapter between that and the host's file system calls |

**MTP has no paths.** Everything is a numbered object with a parent, so a
path is found by walking from the top. A folder's handles are cheap to list;
describing them is what costs, so descriptions are remembered by handle and
only what is new in a folder is described. The first look at a thousand
photographs on a Galaxy A05s takes about 7 seconds (four property lists
instead of a thousand ObjectInfos, which took 14); the next is instant.

**Part of a file is read without the rest** (GetPartialObject64), so a viewer
opens a large video at once. **A file going to the phone is gathered first**:
MTP takes a file as one transfer of a stated length, and the pipe is the
phone's only one. **A copy within the phone is done by the phone**
(CopyObject), with nothing crossing the cable.

**The phone is let go after a minute of nothing**, so that another program
can reach it; the command *Let go of connected phones* does it at once.

## Platforms

macOS only, on Apple silicon and Intel. libusb 1.0.30 is in `native/`, built
for macOS 11 and later by `native/build.sh`. It is LGPL-2.1 and loaded at run
time, never linked. Windows and most Linux desktops read MTP phones
themselves, and there the operating system's driver holds the phone, so
libusb cannot reach it.

## Testing

`python3 selftest.py <the app's assets/python>` with a phone plugged in. It
writes only inside `Download/xverb-mtp-selftest`, which it makes and deletes,
with the sizes that break a USB transfer when the ends are wrong.

## Known limits

- One phone was tested: a Samsung Galaxy A05s on Android 14.
- A file changed on the phone while its folder is open keeps the size it was
  listed with until the panel is reopened after a minute idle.
- Asking a Samsung for every property of a folder at once is refused, late
  enough to jam the pipe, so each property is asked for by name.
