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

"""Skeletons and animations out of Unreal packages.

**A skeleton** is the `Skeleton` object's reference skeleton, written by hand
after its properties: each bone's name, parent and (in the editor) export
name, then each bone's rest pose — doubles from UE 5.0's large coordinates on,
floats before.

**An animation** is kept three different ways across the engine's life, and
each is read here into the same thing — a track of keys per bone name:

- UE4 and the earliest UE5 write `FRawAnimSequenceTrack`s straight after
  the sequence's properties: positions, rotations, scales, each array
  prefixed by the size of one element;
- UE 5.0 and 5.1 move them into an `AnimDataModel` object, as a tagged
  array of `BoneAnimationTrack`s;
- UE 5.2 on keep them as a Sequencer section driving an FK control rig:
  per bone nine float curves — translation, rotation in degrees, scale.
"""

from __future__ import annotations

import math
import struct
from typing import Dict, List, Optional, Tuple

import ueobject
from uasset import Package, UassetError, _Reader


class Skeleton:
    def __init__(self, names: List[str], parents: List[int], pose: List[tuple]):
        self.names = names
        self.parents = parents
        #: Each bone's rest transform, local to its parent: (quat xyzw, translation, scale).
        self.pose = pose


def _transform(r: _Reader, wide: bool) -> tuple:
    fmt = "<4d3d3d" if wide else "<4f3f3f"
    size = struct.calcsize(fmt)
    r.need(size)
    values = struct.unpack_from(fmt, r.data, r.at)
    r.at += size
    return (values[0:4], values[4:7], values[7:10])


def skeleton(p: Package) -> Optional[Skeleton]:
    for export in ueobject.exports(p):
        if export.klass != "Skeleton":
            continue
        _, at = ueobject.properties(p, export)
        r = _Reader(p.data, at)
        if r.i32():
            r.skip(16)  # the object's guid
        count = r.i32()
        if not 0 < count < 10000:
            raise UassetError("The skeleton's bones are damaged.")
        names, parents = [], []
        export_names = not p.editor_only_filtered
        for _ in range(count):
            names.append(p.name(r.i32(), r.i32()))
            parents.append(r.i32())
            if export_names:
                r.string()
        if r.i32() != count:
            raise UassetError("The skeleton's pose does not match its bones.")
        wide = p.ue5 >= ueobject.UE5_LARGE_WORLD_COORDINATES
        pose = [_transform(r, wide) for _ in range(count)]
        return Skeleton(names, parents, pose)
    return None


# -- keys ---------------------------------------------------------------------------

class Track:
    """One bone's keys, a frame apart: positions, rotations (x, y, z, w), scales.

    An empty list keeps the rest pose's value; one key holds for every frame.
    """

    __slots__ = ("positions", "rotations", "scales")

    def __init__(self, positions=None, rotations=None, scales=None):
        self.positions = positions or []
        self.rotations = rotations or []
        self.scales = scales or []


class Animation:
    def __init__(self, tracks: Dict[str, Track], frames: int, fps: float, model: str):
        self.tracks = tracks
        self.frames = max(1, frames)
        self.fps = fps if fps > 0 else 30.0
        #: Which of the three ways it was kept, for the report.
        self.model = model

    @property
    def seconds(self) -> float:
        return (self.frames - 1) / self.fps if self.frames > 1 else 0.0


def _bulk(r: _Reader, fmt: str) -> list:
    """`BulkSerialize`d array: the size of one element, a count, the elements."""
    element = r.i32()
    count = r.i32()
    if count < 0 or element <= 0 or count * element > len(r.data) - r.at:
        raise UassetError("An animation's keys are damaged.")
    size = struct.calcsize("<" + fmt)
    if element != size:
        # Doubles where floats were expected, or the reverse.
        fmt = fmt.replace("f", "d") if element == size * 2 else fmt.replace("d", "f")
    n = len(fmt)
    values = struct.unpack_from("<%d%s" % (count * n, fmt[0]), r.data, r.at)
    r.at += count * element
    return [tuple(values[i:i + n]) for i in range(0, len(values), n)]


def _raw_tracks(p: Package, at: int, count: int) -> List[Track]:
    r = _Reader(p.data, at)
    out = []
    for _ in range(count):
        positions = _bulk(r, "fff")
        rotations = _bulk(r, "ffff")
        scales = _bulk(r, "fff")
        out.append(Track(positions, rotations, scales))
    return out


def _find_raw_start(p: Package, at: int, count: int) -> Optional[int]:
    """Where `RawAnimationData` begins after a sequence's properties.

    The object's guid flag, the skeleton's guid, two bytes of strip flags —
    then the track count and the first element size, 12. Looked for rather
    than walked to, across the few ways the engines put those first fields.
    """
    for start in range(at, min(at + 64, len(p.data) - 12)):
        n, element = struct.unpack_from("<ii", p.data, start)
        if n == count and element in (12, 24):
            return start + 4
    return None


def _vectors(p: Package, tag) -> list:
    count, at, name = ueobject.array_elements(p, tag)
    wide = name in ("Vector", "Vector3d", "Quat", "Quat4d") and p.ue5 >= ueobject.UE5_LARGE_WORLD_COORDINATES
    n = 4 if name and name.startswith("Quat") else 3
    fmt = "<%d%s" % (count * n, "d" if wide else "f")
    values = struct.unpack_from(fmt, p.data, at)
    return [tuple(values[i:i + n]) for i in range(0, len(values), n)]


def _data_model_tracks(p: Package, export) -> Tuple[Dict[str, Track], int, float]:
    tags, _ = ueobject.properties(p, export)
    named = ueobject.by_name(tags)
    tracks: Dict[str, Track] = {}
    if "BoneAnimationTracks" in named:
        tag = named["BoneAnimationTracks"][0]
        count, at, _ = ueobject.array_elements(p, tag)
        r = _Reader(p.data, at)
        for _ in range(count):
            element = ueobject.by_name(ueobject.tags(p, r, tag.at + tag.size))
            name = ueobject.name_value(p, element["Name"][0]) if "Name" in element else ""
            if "InternalTrackData" not in element:
                continue
            inner = element["InternalTrackData"][0]
            keys = ueobject.by_name(ueobject.tags(p, _Reader(p.data, inner.at), inner.at + inner.size))
            tracks[name] = Track(
                _vectors(p, keys["PosKeys"][0]) if "PosKeys" in keys else [],
                _vectors(p, keys["RotKeys"][0]) if "RotKeys" in keys else [],
                _vectors(p, keys["ScaleKeys"][0]) if "ScaleKeys" in keys else [])
    frames = ueobject.int_value(p, named["NumberOfKeys"][0]) if "NumberOfKeys" in named else 0
    fps = ueobject.frame_rate(p, named["FrameRate"][0]) if "FrameRate" in named else 30.0
    return tracks, frames, fps


class Curve:
    """A Sequencer float channel: key times in ticks, values, and a default."""

    __slots__ = ("times", "values", "default", "rate")

    def __init__(self, times, values, default, rate):
        self.times = times
        self.values = values
        self.default = default
        self.rate = rate

    def at(self, tick: float) -> float:
        times = self.times
        if not times:
            return self.default
        if tick <= times[0]:
            return self.values[0]
        if tick >= times[-1]:
            return self.values[-1]
        lo, hi = 0, len(times) - 1
        while hi - lo > 1:
            mid = (lo + hi) // 2
            if times[mid] <= tick:
                lo = mid
            else:
                hi = mid
        span = times[hi] - times[lo]
        f = (tick - times[lo]) / span if span else 0.0
        return self.values[lo] + (self.values[hi] - self.values[lo]) * f


def _curve(p: Package, tag) -> Curve:
    r = _Reader(p.data, tag.at)
    r.skip(2)  # what happens before the first key and after the last
    element = r.i32()
    count = r.i32()
    times = list(struct.unpack_from("<%di" % count, p.data, r.at)) if element == 4 else []
    r.skip(element * count)
    element = r.i32()
    count = r.i32()
    values = [struct.unpack_from("<f", p.data, r.at + i * element)[0] for i in range(count)]
    r.skip(element * count)
    default = struct.unpack_from("<f", p.data, r.at)[0]
    has_default = struct.unpack_from("<i", p.data, r.at + 4)[0]
    num, den = struct.unpack_from("<ii", p.data, r.at + 8)
    return Curve(times[:len(values)], values, default if has_default else 0.0,
                 num / den if den else 0.0)


def _rotator_quat(roll: float, pitch: float, yaw: float) -> tuple:
    """FRotator to FQuat, the engine's own formula (degrees in, x y z w out)."""
    sr, cr = math.sin(math.radians(roll) / 2), math.cos(math.radians(roll) / 2)
    sp, cp = math.sin(math.radians(pitch) / 2), math.cos(math.radians(pitch) / 2)
    sy, cy = math.sin(math.radians(yaw) / 2), math.cos(math.radians(yaw) / 2)
    return (cr * sp * sy - sr * cp * cy,
            -cr * sp * cy - sr * cp * sy,
            cr * cp * sy - sr * sp * cy,
            cr * cp * cy + sr * sp * sy)


def _control_rig_tracks(p: Package, export, fps: float) -> Tuple[Dict[str, Track], int, float]:
    tags, _ = ueobject.properties(p, export)
    named = ueobject.by_name(tags)
    if "TransformParameterNamesAndCurves" not in named:
        return {}, 0, fps
    tag = named["TransformParameterNamesAndCurves"][0]
    count, at, _ = ueobject.array_elements(p, tag)
    r = _Reader(p.data, at)
    bones: Dict[str, List[Curve]] = {}
    last = 0.0
    rate = 0.0
    for _ in range(count):
        element = ueobject.tags(p, r, tag.at + tag.size)
        name = ""
        curves: Dict[Tuple[str, int], Curve] = {}
        for t in element:
            if t.name == "ParameterName":
                name = ueobject.name_value(p, t)
            elif t.name in ("Translation", "Rotation", "Scale"):
                curve = _curve(p, t)
                curves[(t.name, t.index)] = curve
                if curve.times:
                    last = max(last, curve.times[-1] / curve.rate if curve.rate else 0.0)
                    rate = rate or curve.rate
        if name.endswith("_CONTROL"):
            name = name[:-8]
        bones[name] = [curves.get((kind, i)) for kind in ("Translation", "Rotation", "Scale")
                       for i in range(3)]
    frames = int(round(last * fps)) + 1
    tracks: Dict[str, Track] = {}
    for name, curves in bones.items():
        if not any(c is not None and c.times for c in curves):
            continue
        track = Track()
        for frame in range(frames):
            tick = frame / fps * rate if rate else frame
            v = [(c.at(tick) if c is not None else (1.0 if i >= 6 else 0.0))
                 for i, c in enumerate(curves)]
            track.positions.append((v[0], v[1], v[2]))
            track.rotations.append(_rotator_quat(v[3], v[4], v[5]))
            track.scales.append((v[6], v[7], v[8]))
        tracks[name] = track
    return tracks, frames, fps


def animation(p: Package) -> Optional[Animation]:
    """The package's animation, whichever of the three ways it was kept."""
    found = ueobject.exports(p)
    sequence = next((e for e in found if e.klass == "AnimSequence"), None)
    if sequence is None:
        return None
    tags, after = ueobject.properties(p, sequence)
    named = ueobject.by_name(tags)
    fps = 30.0
    for key in ("SamplingFrameRate", "TargetFrameRate", "FrameRate"):
        if key in named:
            fps = ueobject.frame_rate(p, named[key][0])
            break

    rig = next((e for e in found if e.klass == "MovieSceneControlRigParameterSection"), None)
    if rig is not None:
        tracks, frames, fps = _control_rig_tracks(p, rig, fps)
        return Animation(tracks, frames, fps, "sequencer")
    model = next((e for e in found if e.klass == "AnimDataModel"), None)
    if model is not None:
        tracks, frames, rate = _data_model_tracks(p, model)
        return Animation(tracks, frames, rate or fps, "data model")
    names = ueobject.names_array(p, named["AnimationTrackNames"][0]) \
        if "AnimationTrackNames" in named else []
    frames = ueobject.int_value(p, named["NumberOfKeys"][0]) if "NumberOfKeys" in named else \
        ueobject.int_value(p, named["NumFrames"][0]) + 1 if "NumFrames" in named else 0
    if not names:
        return None
    start = _find_raw_start(p, after, len(names))
    if start is None:
        return None
    raw = _raw_tracks(p, start, len(names))
    return Animation(dict(zip(names, raw)), frames, fps, "raw tracks")


def skeleton_path(p: Package) -> str:
    """The package path of the skeleton an animation plays on."""
    imports = p.imports()
    for index, (klass, name, outer) in enumerate(imports):
        if klass != "Skeleton":
            continue
        while outer < 0 and -outer - 1 < len(imports):
            klass, name, outer = imports[-outer - 1]
            if klass == "Package":
                return name
    return ""


# -- poses --------------------------------------------------------------------------

def matrix(rotation, translation, scale) -> List[float]:
    """`FTransform::ToMatrixWithScale`, met by a row vector from the left."""
    x, y, z, w = rotation
    x2, y2, z2 = x + x, y + y, z + z
    xx, xy, xz = x * x2, x * y2, x * z2
    yy, yz, zz = y * y2, y * z2, z * z2
    wx, wy, wz = w * x2, w * y2, w * z2
    sx, sy, sz = scale
    tx, ty, tz = translation
    return [(1 - (yy + zz)) * sx, (xy + wz) * sx, (xz - wy) * sx, 0.0,
            (xy - wz) * sy, (1 - (xx + zz)) * sy, (yz + wx) * sy, 0.0,
            (xz + wy) * sz, (yz - wx) * sz, (1 - (xx + yy)) * sz, 0.0,
            tx, ty, tz, 1.0]


def multiply(a: List[float], b: List[float]) -> List[float]:
    out = [0.0] * 16
    for row in range(4):
        a0, a1, a2, a3 = a[row * 4:row * 4 + 4]
        for col in range(4):
            out[row * 4 + col] = a0 * b[col] + a1 * b[4 + col] + a2 * b[8 + col] + a3 * b[12 + col]
    return out


def _key(keys: list, frame: int, fallback):
    if not keys:
        return fallback
    return keys[frame] if frame < len(keys) else keys[-1]


def world_poses(skeleton: Skeleton, anim: Optional[Animation], frame: int) -> List[List[float]]:
    """Every bone's matrix on the model at a frame; the rest pose without an animation."""
    out: List[List[float]] = []
    for i, name in enumerate(skeleton.names):
        rotation, translation, scale = skeleton.pose[i]
        track = anim.tracks.get(name) if anim is not None else None
        if track is not None:
            rotation = _key(track.rotations, frame, rotation)
            translation = _key(track.positions, frame, translation)
            scale = _key(track.scales, frame, scale)
        local = matrix(rotation, translation, scale)
        parent = skeleton.parents[i]
        out.append(multiply(local, out[parent]) if 0 <= parent < i else local)
    return out


# -- for the host's 3D view ---------------------------------------------------------

#: Unreal is left-handed with Z up; the view is right-handed with Y up and
#: reads no axis setting. Swapping Y and Z does both at once — it stands the
#: model up and, being a mirror, puts a character's left arm on its left
#: again — and it is its own inverse.
_MIRROR = [1.0, 0, 0, 0, 0, 0, 1.0, 0, 0, 1.0, 0, 0, 0, 0, 0, 1.0]

MAX_FRAMES = 300


def invert(m: List[float]) -> List[float]:
    a = [m[i * 4:(i + 1) * 4] + [1.0 if i == j else 0.0 for j in range(4)] for i in range(4)]
    for col in range(4):
        pivot = max(range(col, 4), key=lambda r: abs(a[r][col]))
        if abs(a[pivot][col]) < 1e-12:
            return [1.0 if i % 5 == 0 else 0.0 for i in range(16)]
        a[col], a[pivot] = a[pivot], a[col]
        p = a[col][col]
        a[col] = [v / p for v in a[col]]
        for row in range(4):
            if row != col and a[row][col]:
                f = a[row][col]
                a[row] = [v - f * w for v, w in zip(a[row], a[col])]
    return [a[i][4 + j] for i in range(4) for j in range(4)]


def _mirrored(m: List[float]) -> List[float]:
    return multiply(multiply(_MIRROR, m), _MIRROR)


def rest_bones(skeleton: Skeleton) -> List[float]:
    """Where each bone stands at rest, three numbers a bone, in the view's space."""
    out: List[float] = []
    for m in world_poses(skeleton, None, 0):
        m = _mirrored(m)
        out.extend((m[12], m[13], m[14]))
    return out


def model(skeleton: Skeleton, anim: Optional[Animation], title: str) -> Tuple[dict, list]:
    """The skeleton as a mesh with no triangles, and its clip if it has one —
    the shape the 3D plugin sends a rig without a body in."""
    mesh = {
        "name": title,
        "color": "",
        "positions": [],
        "normals": [],
        "uvs": [],
        "indices": [],
        "joints": len(skeleton.names),
        "bones": rest_bones(skeleton),
        "boneParents": list(skeleton.parents),
    }
    return mesh, clip(skeleton, anim, title)


def clip(skeleton: Skeleton, anim: Optional[Animation], title: str) -> list:
    """A matrix per bone per frame, from the rest pose to the frame's pose."""
    rest = [_mirrored(m) for m in world_poses(skeleton, None, 0)]
    clips = []
    if anim is not None and anim.frames > 1:
        step = max(1, math.ceil(anim.frames / MAX_FRAMES))
        picked = list(range(0, anim.frames, step))
        if picked[-1] != anim.frames - 1:
            picked.append(anim.frames - 1)
        undo = [invert(m) for m in rest]
        matrices: List[float] = []
        for frame in picked:
            for i, m in enumerate(world_poses(skeleton, anim, frame)):
                matrices.extend(multiply(undo[i], _mirrored(m)))
        clips.append({
            "name": title,
            "frames": len(picked),
            "fps": (len(picked) - 1) / anim.seconds if anim.seconds > 0 else anim.fps,
            "seconds": anim.seconds,
            "tracks": [matrices],
        })
    return clips


def preview_mesh_path(p: Package) -> str:
    """The skeletal mesh a skeleton is previewed on, as a package path."""
    for export in ueobject.exports(p):
        if export.klass != "Skeleton":
            continue
        tags, _ = ueobject.properties(p, export)
        for tag in tags:
            if tag.name != "PreviewSkeletalMesh":
                continue
            r = _Reader(p.data, tag.at)
            if tag.size == 4:
                return p.soft_path(r.i32())
            path = p.name(r.i32(), r.i32())
            return path.split(".", 1)[0]
    return ""
