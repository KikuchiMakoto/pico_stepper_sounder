"""
stepper.py - PIO-driven stepper motor driver for RP2040 / MicroPython

ピン配置（A4988ドライバ想定、ENA=ActiveLow）:
  Motor0: Step=1, Dir=0, Ena=3  → PIO0 SM0
  Motor1: Step=5, Dir=4, Ena=7  → PIO0 SM1
  Motor2: Step=9, Dir=8, Ena=11 → PIO0 SM2
  Motor3: Step=13,Dir=12,Ena=15 → PIO0 SM3

変更履歴:
  [fix] rp2.StateMachine global ID = pio_instance * 4 + sm_id
  [fix] _speed_to_ticks: 正しい PIO サイクルカウント式
        T = (2*ticks + 8) / F_pio → ticks = F/(2*pps) - 4
  [fix] FIFO blocking: tx_fifo() チェックで低音での asyncio 停止を防止
  [fix] asyncio.sleep(float) → asyncio.sleep_ms(int)
  [opt] _INTERVAL_MS: 20ms → 5ms（速度更新 50→200回/s、加速度の滑らかさ4倍）
  [fix] steps = math.ceil(...) で全音域ストールゼロ（int では A3 以上で 9% 断続）
"""

import math
import uasyncio as asyncio
from machine import Pin
import rp2


@rp2.asm_pio(set_init=rp2.PIO.OUT_LOW, out_shiftdir=rp2.PIO.SHIFT_RIGHT, autopull=False)
def stepper_pio():
    """
    プロトコル（CPU から 2 ワード送信）:
      Word1 → Y: delay_ticks (パルス間隔)
      Word2 → X: step_count  (ステップ数)

    1ステップあたりのサイクル数（全命令 1cy）:
      jmp(not_x)     1
      set(pins,1)    1
      mov(osr,y)     1
      delay_h loop   ticks + 1  (ticks 回 jump + 1 回 fall-through)
      set(pins,0)    1
      mov(osr,y)     1
      delay_l loop   ticks + 1
      jmp(x_dec)     1
      ─────────────────────────
      合計 = 2*ticks + 8  [cy/step]

      PPS = F_pio / (2*ticks + 8)
      ticks = F_pio / (2*pps) - 4
    """
    pull(block)
    out(y, 32)              # Y = ticks

    pull(block)
    out(x, 32)              # X = steps

    label("step_loop")
    jmp(not_x, "end")       # X==0 → 終了 (1cy)

    set(pins, 1)            # STEP High  (1cy)
    mov(osr, y)             # OSR = ticks (1cy)
    label("delay_h")
    jmp(osr_dec, "delay_h") # ticks+1 cy

    set(pins, 0)            # STEP Low   (1cy)
    mov(osr, y)             # OSR = ticks (1cy)
    label("delay_l")
    jmp(osr_dec, "delay_l") # ticks+1 cy

    jmp(x_dec, "step_loop") # X--, jump if non-zero (1cy)

    label("end")
    # wrap → pull(block) で次パラメータ待機


class StepperMotor:
    """PIO + asyncio によるステッピングモータドライバ。"""

    # (step_pin, dir_pin, ena_pin, pio_instance, sm_id)
    MOTOR_CONFIGS = [
        (1,  0,  3,  0, 0),
        (5,  4,  7,  0, 1),
        (9,  8,  11, 0, 2),
        (13, 12, 15, 0, 3),
    ]

    _INTERVAL_MS  = 5      # 制御ループ周期 [ms]（200回/s、20ms比4倍の加速度解像度）
    _INTERVAL_SEC = 0.005  # 同上 [s]
    _FIFO_DEPTH   = 4      # RP2040 PIO TX FIFO 深さ [words]

    def __init__(self, step_pin_id, dir_pin_id, ena_pin_id,
                 pio_instance=0, sm_id=0, pio_freq=125_000_000):
        self._step_pin = Pin(step_pin_id, Pin.OUT)
        self._dir_pin  = Pin(dir_pin_id,  Pin.OUT)
        self._ena_pin  = Pin(ena_pin_id,  Pin.OUT)

        self._ena_pin.value(1)  # A4988: High = Disabled
        self._dir_pin.value(0)  # CW

        # rp2.StateMachine の第1引数はグローバル SM ID (0-7)
        global_sm_id = pio_instance * 4 + sm_id
        self._sm = rp2.StateMachine(
            global_sm_id,
            stepper_pio,
            freq=pio_freq,
            set_base=self._step_pin,
        )

        self._pio_freq      = pio_freq
        self._target_speed  = 0.0
        self._current_speed = 0.0
        self._accel         = 0.0   # 0 = 即時変化

        self._running = False
        self._task    = None

    # ---- Public API ----

    def enable(self):
        self._ena_pin.value(0)

    def disable(self):
        self._ena_pin.value(1)

    def set_speed(self, pps: float):
        self._target_speed = max(0.0, pps)

    def set_accel(self, accel: float):
        self._accel = max(0.0, accel)

    async def start_task(self):
        if self._running:
            return
        self._running = True
        self._sm.active(1)
        self._task = asyncio.create_task(self._run_loop())

    async def stop_task(self):
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        self._sm.active(0)
        self._current_speed = 0.0
        self._target_speed  = 0.0
        self.disable()

    # ---- Internal ----

    def _speed_to_ticks(self, pps: float) -> int:
        """
        PPS → PIO delay_ticks 変換。
        ticks = F_pio / (2 * pps) - 4
        """
        ticks = int(self._pio_freq / (2.0 * pps) - 4.0)
        return max(0, ticks)

    async def _run_loop(self):
        self._sm.active(1)
        while self._running:
            # 加減速
            if self._accel > 0.0:
                diff = self._target_speed - self._current_speed
                step = self._accel * self._INTERVAL_SEC
                if abs(diff) <= step:
                    self._current_speed = self._target_speed
                elif diff > 0:
                    self._current_speed += step
                else:
                    self._current_speed -= step
            else:
                self._current_speed = self._target_speed

            # PIO へ送出
            if self._current_speed > 0.0:
                ticks = self._speed_to_ticks(self._current_speed)

                # ceil でバッチ時間 ≥ インターバルを保証し、全音域でストールゼロ。
                # int (floor) だと step_period が interval を割り切れない場合に
                # バッチ完了後の次 put まで PIO がアイドルになる（例: A3以上で9%断続）。
                steps = max(1, math.ceil(self._current_speed * self._INTERVAL_SEC))

                # FIFO ブロッキング対策:
                # step_period > interval_ms（低音ノート）では PIO 消化が遅く FIFO が
                # 溜まる。sm.put() はブロッキング API なので満杯時は asyncio が停止する。
                # 空き = FIFO_DEPTH - tx_fifo() が 2 words 以上のときのみ put。
                if self._sm.tx_fifo() <= (self._FIFO_DEPTH - 2):
                    self._sm.put(ticks)
                    self._sm.put(steps)

            await asyncio.sleep_ms(self._INTERVAL_MS)
