// Copyright (C) 2026 xsm909
//
// This file is part of xverb-plugins.
//
// This program is free software: you can redistribute it and/or modify
// it under the terms of the GNU General Public License as published by
// the Free Software Foundation, either version 3 of the License, or
// (at your option) any later version.
//
// This program is distributed in the hope that it will be useful,
// but WITHOUT ANY WARRANTY; without even the implied warranty of
// MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
// GNU General Public License for more details.
//
// You should have received a copy of the GNU General Public License
// along with this program.  If not, see <https://www.gnu.org/licenses/>.

// The one function the HDR plugin calls, around tinyexr.
//
// The plugin has already read the header itself and chosen the part and the
// channels; this decodes that part with tinyexr (threads across blocks) and
// writes the chosen channels into the caller's buffer as float32 planes, one
// after another, each width * height, rows top to bottom. Half becomes float,
// uint becomes float. A tiled part is put together from its level-0 tiles.
//
// Everything the plugin needs to decide lives on the Python side; this knows
// nothing of exposure or layers, and says so with an error rather than
// guessing when what it finds is not what it was told.

#define TINYEXR_IMPLEMENTATION
#define TINYEXR_USE_MINIZ 1
#define TINYEXR_USE_THREAD 1
#define TINYEXR_USE_OPENMP 0
#include "miniz.h"
#include "tinyexr.h"

#include <cstdio>
#include <cstring>
#include <vector>

#if defined(_WIN32)
#define XV_EXPORT extern "C" __declspec(dllexport)
#else
#define XV_EXPORT extern "C" __attribute__((visibility("default")))
#endif

namespace {

void say(char *err, int size, const char *what, const char *detail) {
  if (!err || size <= 0) return;
  std::snprintf(err, (size_t)size, "%s%s%s", what, detail ? ": " : "", detail ? detail : "");
}

float sample(const unsigned char *plane, int type, size_t at) {
  if (type == TINYEXR_PIXELTYPE_FLOAT) return reinterpret_cast<const float *>(plane)[at];
  if (type == TINYEXR_PIXELTYPE_UINT)
    return (float)reinterpret_cast<const unsigned int *>(plane)[at];
  return 0.0f;  // half was asked for as float and never arrives as half
}

int copy_out(const EXRHeader &header, const EXRImage &image, const char *const *names, int count,
             float *out, int width, int height, char *err, int errlen) {
  std::vector<int> index((size_t)count, -1);
  for (int k = 0; k < count; ++k)
    for (int c = 0; c < header.num_channels; ++c)
      if (std::strcmp(header.channels[c].name, names[k]) == 0) index[(size_t)k] = c;
  for (int k = 0; k < count; ++k)
    if (index[(size_t)k] < 0) {
      say(err, errlen, "no such channel", names[k]);
      return -4;
    }
  const size_t plane = (size_t)width * (size_t)height;

  if (!header.tiled) {
    if (image.width != width || image.height != height || !image.images) {
      say(err, errlen, "the part is not the size it was said to be", nullptr);
      return -5;
    }
    for (int k = 0; k < count; ++k) {
      const int c = index[(size_t)k];
      const unsigned char *src = image.images[c];
      const int type = header.pixel_types[c];
      float *dst = out + plane * (size_t)k;
      if (type == TINYEXR_PIXELTYPE_FLOAT) {
        std::memcpy(dst, src, plane * sizeof(float));
      } else {
        for (size_t i = 0; i < plane; ++i) dst[i] = sample(src, type, i);
      }
    }
    return 0;
  }

  // Tiled: level 0 only, which is the top of the linked levels.
  if (!image.tiles) {
    say(err, errlen, "a tiled part with no tiles", nullptr);
    return -5;
  }
  const int tx = header.tile_size_x, ty = header.tile_size_y;
  for (int t = 0; t < image.num_tiles; ++t) {
    const EXRTile &tile = image.tiles[t];
    if (tile.level_x != 0 || tile.level_y != 0) continue;
    const int x0 = tile.offset_x * tx, y0 = tile.offset_y * ty;
    for (int k = 0; k < count; ++k) {
      const int c = index[(size_t)k];
      const int type = header.pixel_types[c];
      float *dst = out + plane * (size_t)k;
      for (int y = 0; y < tile.height; ++y) {
        if (y0 + y >= height) break;
        for (int x = 0; x < tile.width; ++x) {
          if (x0 + x >= width) break;
          // A tile's rows are tile_size_x apart even where the tile is cut
          // short at the right edge.
          dst[(size_t)(y0 + y) * (size_t)width + (size_t)(x0 + x)] =
              sample(tile.images[c], type, (size_t)y * (size_t)tx + (size_t)x);
        }
      }
    }
  }
  return 0;
}

void as_float(EXRHeader &header) {
  for (int c = 0; c < header.num_channels; ++c)
    if (header.pixel_types[c] == TINYEXR_PIXELTYPE_HALF)
      header.requested_pixel_types[c] = TINYEXR_PIXELTYPE_FLOAT;
}

}  // namespace

// 1 while the interface below stays what it is.
XV_EXPORT int xv_version() { return 1; }

// Decodes part [part] of the EXR in [data] and writes channels [names] into
// [out] as float32 planes of [width] * [height]. 0 on success; below 0 with a
// sentence in [err] otherwise.
XV_EXPORT int xv_decode(const unsigned char *data, size_t size, int part, const char *const *names,
                        int count, float *out, int width, int height, char *err, int errlen) {
  if (!data || !names || !out || count <= 0 || width <= 0 || height <= 0) {
    say(err, errlen, "bad arguments", nullptr);
    return -1;
  }
  const char *why = nullptr;
  EXRVersion version;
  if (ParseEXRVersionFromMemory(&version, data, size) != TINYEXR_SUCCESS) {
    say(err, errlen, "not an EXR tinyexr can read", nullptr);
    return -2;
  }

  if (!version.multipart) {
    if (part != 0) {
      say(err, errlen, "a single-part file has no part", nullptr);
      return -1;
    }
    EXRHeader header;
    InitEXRHeader(&header);
    if (ParseEXRHeaderFromMemory(&header, &version, data, size, &why) != TINYEXR_SUCCESS) {
      say(err, errlen, "the header", why);
      FreeEXRErrorMessage(why);
      return -3;
    }
    as_float(header);
    EXRImage image;
    InitEXRImage(&image);
    if (LoadEXRImageFromMemory(&image, &header, data, size, &why) != TINYEXR_SUCCESS) {
      say(err, errlen, "the pixels", why);
      FreeEXRErrorMessage(why);
      FreeEXRHeader(&header);
      return -3;
    }
    const int done = copy_out(header, image, names, count, out, width, height, err, errlen);
    FreeEXRImage(&image);
    FreeEXRHeader(&header);
    return done;
  }

  EXRHeader **headers = nullptr;
  int parts = 0;
  if (ParseEXRMultipartHeaderFromMemory(&headers, &parts, &version, data, size, &why) !=
      TINYEXR_SUCCESS) {
    say(err, errlen, "the headers", why);
    FreeEXRErrorMessage(why);
    return -3;
  }
  int done = -1;
  if (part < 0 || part >= parts) {
    say(err, errlen, "no such part", nullptr);
  } else {
    for (int p = 0; p < parts; ++p) as_float(*headers[p]);
    std::vector<EXRImage> images((size_t)parts);
    for (int p = 0; p < parts; ++p) InitEXRImage(&images[(size_t)p]);
    if (LoadEXRMultipartImageFromMemory(images.data(), const_cast<const EXRHeader **>(headers),
                                        (unsigned int)parts, data, size, &why) != TINYEXR_SUCCESS) {
      say(err, errlen, "the pixels", why);
      FreeEXRErrorMessage(why);
      done = -3;
    } else {
      done = copy_out(*headers[part], images[(size_t)part], names, count, out, width, height, err,
                      errlen);
    }
    for (int p = 0; p < parts; ++p) FreeEXRImage(&images[(size_t)p]);
  }
  for (int p = 0; p < parts; ++p) {
    FreeEXRHeader(headers[p]);
    std::free(headers[p]);
  }
  std::free(headers);
  return done;
}
