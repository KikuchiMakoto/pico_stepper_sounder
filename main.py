"""
main.py - MIDI → ステッピングモーター演奏

変更履歴:
  [opt] play_events スピン閾値 1ms → 500us
        5ms motor タスクは non-blocking (~80us) なため asyncio ジッター ≤ 200us。
        500us はそのマージン 2.5 倍であり十分。
"""

import uasyncio as asyncio
import utime
import math
from stepper import StepperMotor
from midi_parser import parse_midi

# ノート番号 → PPS 変換テーブル（起動時 1 回だけ計算）
_PPS_TABLE = [440.0 * math.pow(2.0, (n - 69) / 12.0) for n in range(128)]


class NoteRouter:
    """先着順 4 音ポリフォニー。"""
    NUM_MOTORS = 4

    def __init__(self, motors):
        self._motors = motors
        self._active = {}
        self._free   = list(range(self.NUM_MOTORS))

    def note_on(self, note: int):
        if note in self._active or not self._free:
            return
        motor_idx = self._free.pop(0)
        self._active[note] = motor_idx
        self._motors[motor_idx].set_speed(_PPS_TABLE[note])

    def note_off(self, note: int):
        if note not in self._active:
            return
        motor_idx = self._active.pop(note)
        self._motors[motor_idx].set_speed(0.0)
        self._free.append(motor_idx)

    def all_off(self):
        for m in self._motors:
            m.set_speed(0.0)
        self._active.clear()
        self._free = list(range(self.NUM_MOTORS))


async def play_events(events, router: NoteRouter):
    """
    イベントドリブン再生。

    タイミング戦略:
      > 500ms     : asyncio.sleep_ms(400) で分割 yield
      > 500us     : asyncio.sleep_ms で target-500us まで待機
      残り 500us  : utime スピン（asyncio ジッター吸収）

    スピン閾値 500us の根拠:
      motor タスクは non-blocking（sm.put は FIFO guard により即時）。
      1タスクの実行時間 ≤ 100us、スケジューラオーバーヘッド ≤ 50us。
      実用ジッター ≤ 200us に対してマージン 2.5 倍。
    """
    if not events:
        return

    _SPIN_THRESHOLD_US = 500

    start_us = utime.ticks_us()

    for time_us, ev, note in events:
        now_us  = utime.ticks_diff(utime.ticks_us(), start_us)
        wait_us = time_us - now_us

        while wait_us > 500_000:
            await asyncio.sleep_ms(400)
            now_us  = utime.ticks_diff(utime.ticks_us(), start_us)
            wait_us = time_us - now_us

        if wait_us > _SPIN_THRESHOLD_US:
            await asyncio.sleep_ms((wait_us - _SPIN_THRESHOLD_US) // 1_000)

        # 残り 500us をスピン
        while utime.ticks_diff(utime.ticks_us(), start_us) < time_us:
            pass

        if ev == 'on':
            router.note_on(note)
        else:
            router.note_off(note)

    router.all_off()


async def main(midi_path: str = "/midi/song.mid"):
    print("Loading:", midi_path)
    try:
        with open(midi_path, 'rb') as f:
            data = f.read()
    except OSError as e:
        print("File open error:", e)
        return

    print(" ", len(data), "bytes")

    try:
        events = parse_midi(data)
    except ValueError as e:
        print("MIDI parse error:", e)
        return

    data = None  # GC に返す

    print(" ", len(events), "note events")
    if events:
        print("  Duration:", events[-1][0] // 1_000_000, "s")

    motors = []
    for step, dir_, ena, pio, sm in StepperMotor.MOTOR_CONFIGS:
        m = StepperMotor(step, dir_, ena, pio_instance=pio, sm_id=sm)
        m.set_accel(0)
        m.enable()
        motors.append(m)

    for m in motors:
        await m.start_task()

    router = NoteRouter(motors)
    print("Playing...")
    await play_events(events, router)
    print("Done.")

    for m in motors:
        await m.stop_task()


try:
    asyncio.run(main("/midi/song.mid"))
except KeyboardInterrupt:
    print("Interrupted.")
finally:
    asyncio.new_event_loop()
