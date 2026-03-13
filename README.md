# midi-stepper

MicroPython (RP2040) で MIDI ファイルをステッピングモーターの駆動音として再生するプロジェクト。

## ハードウェア

- Raspberry Pi Pico / Pico W (RP2040)
- A4988 ステッピングモータードライバー × 4
- ステッピングモーター（1.8°/200steps/rev）× 4

### ピン配置

| Motor | STEP | DIR | ENA | PIO SM |
|-------|------|-----|-----|--------|
| 0     | 1    | 0   | 3   | PIO0-0 |
| 1     | 5    | 4   | 7   | PIO0-1 |
| 2     | 9    | 8   | 11  | PIO0-2 |
| 3     | 13   | 12  | 15  | PIO0-3 |

## ファイル構成

| ファイル | 説明 |
|---|---|
| `stepper.py` | PIO ベースのステッピングモータードライバー |
| `midi_parser.py` | SMF Type 0/1 対応 MIDI パーサー |
| `main.py` | MIDI 再生 + ノート→モーター割り当て |

## 使い方

```bash
# MIDI ファイルと .py を転送
mpremote fs mkdir /midi
mpremote fs cp song.mid :/midi/song.mid
mpremote cp stepper.py :
mpremote cp midi_parser.py :
mpremote cp main.py :

# 実行
mpremote run main.py
```

## 仕様

- SMF Type 0 / Type 1 対応（Type 2 非対応）
- SMPTE タイムコード非対応
- 最大同時発音数: 4（先着順、あふれは無視）
- テンポチェンジ対応
- ドラムトラック（ch 9）はフィルタしていない
