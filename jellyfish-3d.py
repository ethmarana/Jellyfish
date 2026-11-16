#!/usr/bin/env python3
"""A fullscreen 3D ASCII jellyfish with white highlights."""

import argparse
import curses
import math
import os
import random
import select
import signal
import sys
import termios
import time
import tty
from functools import lru_cache
from itertools import groupby
from operator import itemgetter


FPS = 30
ESC = "\x1b["
ENTER_SCREEN = ESC + "?1049h" + ESC + "?25l"
LEAVE_SCREEN = ESC + "0m" + ESC + "?1049l" + ESC + "?25h"
WATER = (4, 16, 18)
WATER_COLORS = (
    (19, 38, 37), (27, 54, 48), (41, 72, 61),
    (54, 92, 76), (84, 130, 104),
)
# Shadow, midtone, and highlight as the color wave cycles around the body.
COLOR_CYCLE = (
    ((102, 54, 16), (178, 82, 37), (228, 206, 155)),
    ((118, 62, 21), (205, 109, 49), (245, 221, 170)),
    ((121, 98, 45), (208, 163, 82), (246, 226, 179)),
    ((106, 106, 54), (188, 182, 116), (236, 228, 178)),
    ((91, 99, 47), (161, 136, 36), (228, 206, 155)),
    ((41, 77, 62), (71, 112, 85), (188, 182, 116)),
)
COLOR_STATIONS = 25
COLOR_SECTORS = 24
SHADES = 16
REFLECTION_STEPS = 8
CUBE_STEPS = (0, 95, 135, 175, 215, 255)
CUBE_INDEX = tuple(min(range(6), key=lambda i: abs(CUBE_STEPS[i] - v)) for v in range(256))
TEXTURE = (".'", ".,:", ":!i;", "il1|", "trc+=v", "xczsu<>", "aen0o", "BO08S", "8%@$#")

# Origin around the rim, length, and number of neighboring strands.
FILAMENTS = (
    (-2.91, 3.42, 3), (-1.85, 3.45, 2), (-0.78, 3.58, 2),
    (0.31, 3.43, 2), (1.40, 3.64, 3), (2.42, 3.31, 2),
)
ARMS = ((0.31, 2.65, 0.24), (2.52, 2.92, 0.28), (4.51, 2.42, 0.24))


def clamp(value):
    return max(0.0, min(1.0, value))


def smooth(value):
    value = clamp(value)
    return value * value * (3 - 2 * value)


def mix(a, b, amount):
    return tuple(round(low + (high - low) * amount) for low, high in zip(a, b))


def indexed_color(rgb):
    r, g, b = rgb
    ri, gi, bi = CUBE_INDEX[r], CUBE_INDEX[g], CUBE_INDEX[b]
    gray = max(0, min(23, round((sum(rgb) / 3 - 8) / 10)))
    gray_value = 8 + 10 * gray
    gray_error = (r - gray_value)**2 + (g - gray_value)**2 + (b - gray_value)**2
    cube_error = (r - CUBE_STEPS[ri])**2 + (g - CUBE_STEPS[gi])**2 + (b - CUBE_STEPS[bi])**2
    return 232 + gray if gray_error < cube_error else 16 + 36 * ri + 6 * gi + bi


def color_mode(fd):
    if os.environ.get("NO_COLOR"):
        return 0
    term = os.environ.get("TERM", "")
    colorterm = os.environ.get("COLORTERM", "").lower()
    if colorterm in {"truecolor", "24bit"} or term.endswith(("-direct", "-truecolor", "-24bit")):
        return 1 << 24
    try:
        curses.setupterm(fd=fd)
    except curses.error:
        return 256 if "256color" in term else 8
    return max(0, curses.tigetnum("colors"))


def palette(elapsed, mode):
    if mode < 8:
        return "", lambda _: ""

    def ansi(rgb, channel):
        if mode >= 1 << 24:
            r, g, b = rgb
            return f"{ESC}{channel};2;{r};{g};{b}m"
        return f"{ESC}{channel};5;{indexed_color(rgb)}m"

    background = ESC + "49m"
    @lru_cache(maxsize=None)
    def colors_at(station, sector):
        distance = station / (COLOR_STATIONS - 1)
        # Offset the cycle both around the body and along it to move hues in a spiral.
        cycle = (elapsed / 7.6 + 0.46 - 0.80 * distance - sector / COLOR_SECTORS) * len(COLOR_CYCLE)
        stop = math.floor(cycle)
        current_inks = COLOR_CYCLE[stop % len(COLOR_CYCLE)]
        next_inks = COLOR_CYCLE[(stop + 1) % len(COLOR_CYCLE)]
        blend = smooth(cycle - stop)
        shadow, mid, highlight = (mix(a, b, blend) for a, b in zip(current_inks, next_inks))
        stage = (elapsed / 4.25 + 0.32 - 0.76 * distance) % 1
        crest = 0.28 * math.exp(-((stage - 0.145) / 0.066)**2)
        return shadow, mid, highlight, crest

    @lru_cache(maxsize=None)
    def color_for(code):
        if code < len(WATER_COLORS):
            return ansi(WATER_COLORS[code], 38) if mode >= 256 else ESC + "36m"

        shaded, reflected = divmod(code - len(WATER_COLORS), REFLECTION_STEPS + 1)
        position, shade = divmod(shaded, SHADES)
        station, sector = divmod(position, COLOR_SECTORS)
        shadow, mid, highlight, crest = colors_at(station, sector)
        reflection = reflected / REFLECTION_STEPS
        light = clamp(shade / (SHADES - 1) + crest)
        if mode < 256:
            if light + 0.37 * reflection > 0.74:
                return ESC + "37m"
            return ESC + ("33m" if mid[0] > mid[1] else "32m")

        tint = mix(WATER, shadow, clamp(light / 0.30))
        tint = mix(tint, mid, clamp((light - 0.30) / 0.40))
        rgb = mix(tint, highlight, smooth((light - 0.49) / 0.49))
        if reflected:
            silver = round(255 * clamp(0.14 + 0.98 * light + 0.37 * reflection))
            rgb = mix(rgb, (silver, silver, min(255, silver + 5)), reflection)
        return ansi(rgb, 38)

    return background, color_for


def ink(distance, light, reflection=0, azimuth=0):
    station = round(clamp(distance) * (COLOR_STATIONS - 1))
    sector = round(azimuth / math.tau * COLOR_SECTORS) % COLOR_SECTORS
    shade = round(clamp(light) * (SHADES - 1))
    reflected = round(clamp(reflection) * REFLECTION_STEPS)
    shaded = (station * COLOR_SECTORS + sector) * SHADES + shade
    return len(WATER_COLORS) + shaded * (REFLECTION_STEPS + 1) + reflected


def glyph_for(light, grain):
    glyphs = TEXTURE[min(len(TEXTURE) - 1, int(light * len(TEXTURE)))]
    return glyphs[grain % len(glyphs)]


class Frame:
    def __init__(self, width, height):
        self.width = width
        self.height = height
        self.rows = [[(" ", -1)] * width for _ in range(height)]
        self.depth = [[-math.inf] * width for _ in range(height)]

    def put(self, x, y, glyph, color, z=-10):
        if 0 <= x < self.width and 0 <= y < self.height and z >= self.depth[y][x]:
            self.depth[y][x] = z
            self.rows[y][x] = (glyph, color)

    def encode(self, background, color_for, resized):
        chunks = [background]
        if resized:
            chunks.append(ESC + "2J")
        last_color = ""
        for y, row in enumerate(self.rows, 1):
            chunks.append(f"{ESC}{y};1H")
            for color, cells in groupby(row, itemgetter(1)):
                if color >= 0:
                    code = color_for(color)
                    if code != last_color:
                        chunks.append(code)
                        last_color = code
                chunks.append("".join(glyph for glyph, _ in cells))
        return "".join(chunks)


def make_bubbles():
    rng = random.Random(24)
    return [tuple(rng.random() for _ in range(5)) for _ in range(220)]


def draw_bubbles(frame, bubbles, elapsed):
    count = min(len(bubbles), max(5, frame.width * frame.height // 64))
    for across, start, depth, phase, size in bubbles[:count]:
        y = round(((start - elapsed * (0.016 + 0.058 * depth)) % 1.10 - 0.05) * frame.height)
        x = round((across + 0.025 * math.sin(elapsed * (0.49 + depth) + math.tau * phase)) * frame.width)
        color = int(4.3 * depth)
        if size > 0.969 and frame.width >= 56:
            for dy, row in enumerate((" .-. ", "(   )", " '-' "), -1):
                for dx, glyph in enumerate(row, -2):
                    if glyph != " ":
                        frame.put(x + dx, y + dy, glyph, min(4, color + 1))
        else:
            glyph = "o" if size > 0.87 else "+" if size > 0.74 else "'" if size > 0.57 else "."
            frame.put(x, y, glyph, color)


def contraction(elapsed):
    return (0.5 + 0.5 * math.cos(math.tau * elapsed / 3.68 - 1.92))**3


def spin(elapsed):
    return 0.37 * elapsed + 0.24 * math.sin(0.69 * elapsed)


class Camera:
    def __init__(self, frame, elapsed):
        aspect = 2.04 + 0.55 * clamp((frame.width / (2 * frame.height) - 1) * 1.5)
        self.sy = max(0.15, min((frame.height - 1) / 5.19, frame.width / (aspect * 3.6)))
        self.sx = self.sy * aspect
        self.cx = (frame.width - 1) * 0.463 + self.sx * 0.16 * math.sin(elapsed * 0.61)
        self.cy = (frame.height - 1) / 2 + self.sy * (
            -1.12 + 0.12 * math.sin(math.tau * elapsed / 3.68 + 0.83)
        )
        pitch = 0.25 + 0.115 * math.sin(elapsed * 0.54 + 0.8)
        roll = -0.104 + 0.15 * math.sin(elapsed * 0.55 + 0.2)
        self.sp, self.cp = math.sin(pitch), math.cos(pitch)
        self.sr, self.cr = math.sin(roll), math.cos(roll)

    def rotate(self, x, y, z):
        tilted_y = self.cp * y - self.sp * z
        return (
            self.cr * x - self.sr * tilted_y,
            self.sr * x + self.cr * tilted_y,
            self.sp * y + self.cp * z,
        )

    def project(self, x, y, z):
        x, y, z = self.rotate(x, y, z)
        return self.cx + self.sx * x, self.cy + self.sy * y, z


def paint_bell(frame, camera, radius, rise, elapsed):
    # Intersect one view ray per cell with the upper half of a tilted ellipsoid.
    rz = 0.94 * radius
    rx2, ry2, rz2 = radius**-2, rise**-2, rz**-2
    a = camera.sp**2 * ry2 + camera.cp**2 * rz2
    extent_x = math.hypot(camera.cr * radius, camera.sr * camera.cp * rise, camera.sr * camera.sp * rz)
    extent_y = math.hypot(camera.sr * radius, camera.cr * camera.cp * rise, camera.cr * camera.sp * rz)
    yaw = spin(elapsed)
    left = max(0, math.ceil(camera.cx - camera.sx * extent_x))
    right = min(frame.width - 1, math.floor(camera.cx + camera.sx * extent_x))
    top = max(0, math.ceil(camera.cy - camera.sy * extent_y))
    bottom = min(frame.height - 1, math.ceil(camera.cy + camera.sy * radius * 0.66))

    for row in range(top, bottom + 1):
        screen_y = (row - camera.cy) / camera.sy
        for col in range(left, right + 1):
            screen_x = (col - camera.cx) / camera.sx
            x = camera.cr * screen_x + camera.sr * screen_y
            level = -camera.sr * screen_x + camera.cr * screen_y
            y0, z0 = camera.cp * level, -camera.sp * level
            b = y0 * camera.sp * ry2 + z0 * camera.cp * rz2
            c = x*x * rx2 + y0*y0 * ry2 + z0*z0 * rz2 - 1
            discriminant = b*b - a * c
            if discriminant < 0:
                continue
            depth = math.sqrt(discriminant)
            front = (-b + depth) / a
            y = y0 + front * camera.sp
            if y > 0:
                front = (-b - depth) / a
                y = y0 + front * camera.sp
                if y > 0:
                    continue

            z = z0 + front * camera.cp
            nx, ny, nz = camera.rotate(x * rx2, y * ry2, z * rz2)
            normal = math.sqrt(nx*nx + ny*ny + nz*nz)
            nx, ny, nz = nx / normal, ny / normal, nz / normal
            diffuse = clamp(-0.57 * nx - 0.61 * ny + 0.55 * nz)
            shine = clamp(-0.30 * nx - 0.34 * ny + 0.89 * nz)**17
            azimuth = math.atan2(z / rz, x / radius) - yaw
            down = clamp(1 + y / rise)
            vein = (0.5 + 0.5 * math.cos(12 * azimuth + 4.1 * down))**8
            groove = 0.5 + 0.5 * math.sin(27 * down + 2.8 * math.cos(3 * azimuth))
            grain = (round(54 * azimuth) * 769 + round(53 * down) * 997) % 1543
            edge = (1 - abs(nz))**4
            texture = (grain % 43) / 42
            # A cool key light catches the bell's shoulder and the edge facing it.
            shoulder = smooth((diffuse + 0.025 * (texture - 0.5) - 0.813) / 0.167)
            edge_glint = 0.53 * edge * clamp(-nx + 0.28) * clamp(0.48 - ny)
            reflection = max(shoulder, edge_glint)
            light = 0.146 + 0.60 * diffuse**2.12 + 0.26 * shine
            light += 0.085 * vein + 0.048 * groove + 0.077 * texture + 0.115 * edge
            light += 0.08 * shoulder
            light *= 1 - 0.18 * down**3
            if nz < 0:
                light *= 0.64
            glyph = glyph_for(light, grain // 7)
            frame.put(col, row, glyph, ink(down * 0.24, light, reflection, azimuth), front)


def trace(frame, points):
    for start, finish in zip(points, points[1:]):
        x, y, z, along, light, reflection, azimuth = start
        dx, dy, dz, dc, dl, dr, da = (b - a for a, b in zip(start, finish))
        if abs(dx) > 6.1 * abs(dy):
            glyph = "-"
        elif abs(dx) > 0.37 * abs(dy):
            glyph = "\\" if dx * dy >= 0 else "/"
        else:
            glyph = "|"
        steps = max(1, math.ceil(max(abs(dx), abs(dy))))
        for step in range(steps):
            t = step / steps
            color = ink(along + dc * t, light + dl * t, reflection + dr * t, azimuth + da * t)
            frame.put(round(x + dx * t), round(y + dy * t), glyph, color, z + dz * t)


def paint_rim(frame, camera, radius, elapsed):
    yaw = spin(elapsed)
    samples = max(34, round(7.8 * radius * camera.sx))
    for ring, depression in ((0.87, 0.064), (0.997, 0.016)):
        for i in range(samples + 1):
            angle = i / samples * math.tau
            r = radius * ring
            scallop = math.sin(9 * (angle - yaw))
            ruffle = 0.051 * scallop
            x, y, z = camera.project(r * math.cos(angle), depression + ruffle, 0.94 * r * math.sin(angle))
            light = 0.155 + 0.245 * clamp(0.53 + 0.62 * z) + 0.066 * scallop
            glyph = ":" if scallop > 0.47 else "." if scallop > -0.25 else "'"
            reflection = 0.64 * smooth((scallop - 0.57) / 0.43) * clamp(z + 0.29)
            frame.put(round(x), round(y), glyph, ink(0.245, light, reflection, angle - yaw), z + 0.014)


def current(reach, fraction, elapsed, phase):
    return reach * 0.139 + fraction * (
        0.29 * math.sin(elapsed * 1.17 - 2.42 * reach + 0.37)
        + 0.15 * fraction * math.sin(elapsed * 0.92 - 4.12 * reach + phase)
    )


def paint_filaments(frame, camera, radius, elapsed):
    samples = max(22, round(camera.sy * 6.2))
    for angle, length, count in FILAMENTS:
        if frame.width < 26:
            count = 1
        for strand in range(count):
            lane = strand - (count - 1) / 2
            strand_length = length + 0.096 * lane
            points = []
            for i in range(samples + 1):
                progress = i / samples
                reach = strand_length * progress
                lag = elapsed - 0.57 * reach
                rotation = spin(lag)
                bearing = angle + rotation + 0.103 * lane + progress * (
                    0.57 * math.sin(1.23 * elapsed - 2.95 * reach + angle)
                    + 0.15 * math.sin(1.42 * elapsed - 5.11 * reach - angle)
                )
                r = radius * (0.958 - 0.39 * smooth(1.55 * progress) + 0.30 * progress**4)
                r += 0.18 * progress * math.sin(1.32 * elapsed - 3.74 * reach + 2 * angle)
                px = r * math.cos(bearing) + current(reach, progress, elapsed, angle)
                px += lane * (0.035 + 0.045 * progress)
                py = 0.013 + reach * (1 - 0.035 * contraction(lag))
                pz = 0.93 * r * math.sin(bearing)
                x, y, z = camera.project(px, py, pz)
                facing = clamp(0.53 + 0.59 * z)
                light = 0.245 + 0.44 * facing - 0.17 * progress
                light += 0.065 * math.sin(3.6 * reach - elapsed * 1.22 + angle)
                bend_light = 0.5 + 0.5 * math.cos(2.04 * reach - 0.84 * elapsed + angle + 0.6 * lane)
                reflection = 0.18 + 0.80 * facing**1.65 * (0.50 + 0.50 * bend_light)
                points.append((x, y, z, 0.24 + reach * 0.197, light, reflection, bearing - rotation))
            trace(frame, points)
            x, y, z, along, light, reflection, azimuth = points[-1]
            glyph = "*" if light > 0.46 else "."
            frame.put(round(x), round(y), glyph, ink(along, light + 0.13, reflection, azimuth), z + 0.015)


def paint_oral_arms(frame, camera, elapsed):
    # Frilled membranes twist by different amounts down each arm.
    samples = max(22, round(camera.sy * 4.8))
    cross_samples = max(4, round(camera.sx * 0.31))
    for phase, length, thickness in ARMS:
        for i in range(samples):
            fraction = i / samples
            reach = length * fraction
            lag = elapsed - 0.48 * reach
            rotation = spin(lag)
            angle = phase + rotation + 4.4 * fraction
            r = 0.25 + 0.093 * math.sin(reach * 2.4 + phase) + 0.08 * fraction
            px = r * math.cos(angle) + 0.86 * current(reach, fraction, elapsed, phase)
            py = reach - 0.038 + 0.036 * math.sin(lag * 2.15 + phase)
            pz = r * math.sin(angle)
            twist = angle + 7.2 * fraction + 0.47 * math.sin(2.1 * lag + phase)
            sided_x, sided_z = math.cos(twist), math.sin(twist)
            pleat = 0.69 + 0.31 * math.sin(29 * fraction - 1.21 * elapsed + phase)
            width = thickness * (0.70 + 0.39 * pleat) * (1 - 0.52 * fraction)
            width *= min(1, (1 - fraction) * 8)**0.7
            cx, cy, cz = camera.project(px, py, pz)
            dx, dy, dz = camera.rotate(sided_x * width, pleat * 0.069, sided_z * width)
            fold_x, fold_y, fold_z = camera.rotate(0, 0, 0.05 * math.sin(43 * fraction + phase))
            along = 0.24 + reach * 0.197
            facing = abs(sided_x)
            body_light = 0.24 + 0.15 * facing + 0.10 * pleat - 0.10 * fraction
            key_light = 0.59 * facing + 0.33 * pleat - 0.56
            for j in range(-cross_samples, cross_samples + 1):
                across = j / cross_samples
                fold = 1 - across * across
                x = cx + camera.sx * (across * dx + fold * fold_x)
                y = cy + camera.sy * (across * dy + fold * fold_y)
                z = cz + across * dz + fold * fold_z
                grain = (i * 997 + j * 461 + round(phase * 76)) % 1543
                light = body_light + 0.10 * (grain % 37) / 36 + 0.17 * abs(across)**4 + 0.11 * z
                sheen = smooth((key_light + 0.10 * z) / 0.39)
                reflection = sheen * (0.18 + 0.72 * abs(across)**3)
                azimuth = angle - rotation + 0.38 * across
                frame.put(round(x), round(y), glyph_for(light, grain // 9), ink(along, light, reflection, azimuth), z)


def draw_jellyfish(frame, elapsed):
    camera = Camera(frame, elapsed)
    pulse = contraction(elapsed)
    radius = 1.028 - 0.155 * pulse
    rise = 1.18 + 0.196 * pulse
    paint_filaments(frame, camera, radius, elapsed)
    paint_oral_arms(frame, camera, elapsed)
    paint_bell(frame, camera, radius, rise, elapsed)
    paint_rim(frame, camera, radius, elapsed)


def wait_for_quit(input_fd, deadline):
    while True:
        remaining = max(0, deadline - time.monotonic())
        if not select.select([input_fd], [], [], remaining)[0]:
            return False
        keys = os.read(input_fd, 128)
        if not keys or b"q" in keys.lower():
            return True
        if time.monotonic() >= deadline:
            return False


def animate(input_fd, output):
    mode = color_mode(output.fileno())
    bubbles = make_bubbles()
    previous_size = None
    started = deadline = time.monotonic()
    saved_mode = termios.tcgetattr(input_fd)
    leave = LEAVE_SCREEN
    if os.environ.get("TERM") == "linux":
        leave += ESC + "2J" + ESC + "H"
    try:
        tty.setcbreak(input_fd)
        output.write(ENTER_SCREEN)
        while True:
            size = os.get_terminal_size(output.fileno())
            if size.columns >= 2 and size.lines >= 1:
                elapsed = time.monotonic() - started
                # Leave the rightmost column free to avoid terminal auto-wrap.
                frame = Frame(size.columns - 1, size.lines)
                draw_bubbles(frame, bubbles, elapsed)
                draw_jellyfish(frame, elapsed)
                background, color_for = palette(elapsed, mode)
                output.write(frame.encode(background, color_for, size != previous_size))
                output.flush()
                previous_size = size

            deadline = max(deadline + 1 / FPS, time.monotonic())
            if wait_for_quit(input_fd, deadline):
                return
    finally:
        try:
            output.write(leave)
            output.flush()
        finally:
            termios.tcsetattr(input_fd, termios.TCSANOW, saved_mode)


def finish(signum, _frame):
    raise SystemExit(128 + signum)


class SuspendAnimation(Exception):
    pass


def suspend(_signum, _frame):
    raise SuspendAnimation


def main():
    parser = argparse.ArgumentParser(description=__doc__, epilog="Press q or Ctrl+C to exit.")
    parser.parse_args()
    if not sys.stdin.isatty() or not sys.stdout.isatty() or os.environ.get("TERM") == "dumb":
        parser.exit(1, "jellyfish needs an interactive terminal.\n")

    signal.signal(signal.SIGTERM, finish)
    signal.signal(signal.SIGHUP, finish)
    signal.signal(signal.SIGTSTP, suspend)
    while True:
        try:
            animate(sys.stdin.fileno(), sys.stdout)
            return 0
        except SuspendAnimation:
            os.kill(os.getpid(), signal.SIGSTOP)
        except KeyboardInterrupt:
            return 130


if __name__ == "__main__":
    sys.exit(main())
