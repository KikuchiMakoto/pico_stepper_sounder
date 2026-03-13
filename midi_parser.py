"""
midi_parser.py - MicroPython 向け最小 MIDI パーサー

対応フォーマット: SMF Type 0, Type 1（Type 2 は非対応）
SMPTE タイムコードは非対応（division の MSB が 0 のもののみ）

変更履歴:
  [fix] Aftertouch (0xA0) が Note Off として記録されるバグを修正
        0x80 / 0x90 / 0xA0 を独立した分岐で処理
  [fix] memoryview スライスの bytes 比較を bytes() でラップ
        MicroPython では mv[a:b] == bytes リテラルが TypeError になる場合がある
  [opt] ticks_to_us を O(n_events × n_tempo) から O(n_events + n_tempo) に改善
        ソート済みイベント列を 1 パスで処理するスイープ方式に変更
"""


def _read_vlq(buf: memoryview, pos: int):
    """Variable Length Quantity デコード。(value, next_pos) を返す。"""
    value = 0
    while True:
        b = buf[pos]
        pos += 1
        value = (value << 7) | (b & 0x7F)
        if not (b & 0x80):
            return value, pos


def _parse_track(buf: memoryview):
    """
    1 トラック分のイベントをパース。

    Returns
    -------
    list of (abs_tick, event_type, data)
      event_type 0x90 : Note On  → data = (note, velocity)
      event_type 0x80 : Note Off → data = (note,)
      event_type 0xFF51: Tempo   → data = (tempo_us,)
    """
    events = []
    pos = 0
    length = len(buf)
    abs_tick = 0
    running_status = 0

    while pos < length:
        delta, pos = _read_vlq(buf, pos)
        abs_tick += delta

        b = buf[pos]

        if b == 0xFF:
            # --- Meta イベント ---
            pos += 1
            meta_type = buf[pos]; pos += 1
            meta_len, pos = _read_vlq(buf, pos)

            if meta_type == 0x51 and meta_len == 3:
                tempo = (buf[pos] << 16) | (buf[pos + 1] << 8) | buf[pos + 2]
                events.append((abs_tick, 0xFF51, (tempo,)))

            pos += meta_len
            running_status = 0  # Meta の後はランニングステータスをリセット

        elif b in (0xF0, 0xF7):
            # --- SysEx: 読み飛ばし ---
            pos += 1
            sysex_len, pos = _read_vlq(buf, pos)
            pos += sysex_len
            running_status = 0  # SysEx の後もリセット

        else:
            # --- 通常 MIDI イベント（ランニングステータスあり） ---
            if b & 0x80:
                running_status = b
                pos += 1
            # running_status == 0 はファイル先頭が不正なケース。
            # status = 0 → ev_type = 0 → 全分岐スルー → 1バイトスキップで回復試行。
            status  = running_status
            ev_type = status & 0xF0

            if ev_type == 0x90:
                # Note On
                note = buf[pos]; pos += 1
                vel  = buf[pos]; pos += 1
                if vel > 0:
                    events.append((abs_tick, 0x90, (note, vel)))
                else:
                    # velocity == 0 は Note Off として扱う（MIDI 仕様）
                    events.append((abs_tick, 0x80, (note,)))

            elif ev_type == 0x80:
                # Note Off
                note = buf[pos]; pos += 1
                pos += 1  # velocity は読み捨て
                events.append((abs_tick, 0x80, (note,)))

            elif ev_type == 0xA0:
                # Polyphonic Aftertouch: 2バイト消費するだけ（イベント生成しない）
                pos += 2

            elif ev_type in (0xB0, 0xE0):
                # CC / Pitch Bend: 2バイト
                pos += 2

            elif ev_type in (0xC0, 0xD0):
                # Program Change / Channel Pressure: 1バイト
                pos += 1

            else:
                # 未知 or running_status=0 の場合のリカバリ
                # 1バイト読み飛ばしてパース継続を試みる
                if pos < length:
                    pos += 1

    return events


def parse_midi(data: bytes):
    """
    MIDI バイナリを解析し、ノートイベントリストを返す。

    Parameters
    ----------
    data : bytes
        open(..., 'rb').read() で読み込んだ生データ

    Returns
    -------
    list of (time_us: int, event: str, note: int)
      event は 'on' または 'off'
      time_us は曲頭からの絶対時刻 [μs]（テンポチェンジ反映済み）

    Raises
    ------
    ValueError
        MThd/MTrk シグネチャ不正、SMPTE タイムコード等
    """
    mv = memoryview(data)

    # ---- MThd ヘッダ解析 ----
    # bytes() でラップ: MicroPython では memoryview スライスの
    # bytes リテラル比較が TypeError になる場合がある
    if bytes(mv[0:4]) != b'MThd':
        raise ValueError("Not a MIDI file (MThd not found)")

    hdr_len  = (mv[4] << 24) | (mv[5] << 16) | (mv[6] << 8) | mv[7]
    fmt      = (mv[8]  << 8)  | mv[9]
    n_tracks = (mv[10] << 8)  | mv[11]
    division = (mv[12] << 8)  | mv[13]

    if division & 0x8000:
        raise ValueError("SMPTE timecode division not supported")
    ppqn = division  # ticks per quarter note

    # ---- MTrk チャンク収集 ----
    pos = 8 + hdr_len  # MThd 4byte + length 4byte + hdr_len byte
    track_bufs = []

    for _ in range(n_tracks):
        if bytes(mv[pos:pos + 4]) != b'MTrk':
            raise ValueError("MTrk chunk not found at pos " + str(pos))
        trk_len = (mv[pos+4] << 24) | (mv[pos+5] << 16) | (mv[pos+6] << 8) | mv[pos+7]
        pos += 8
        track_bufs.append(mv[pos:pos + trk_len])
        pos += trk_len

    # ---- 音楽トラックのインデックス決定 ----
    # Type 0: 全データが Track 0 に入っている
    # Type 1: Track 0 = テンポマップ専用、Track 1 以降が音楽
    if fmt == 0:
        music_track_idx = 0
    else:
        music_track_idx = 1

    if music_track_idx >= len(track_bufs):
        raise ValueError(
            "Music track index {} not found (n_tracks={})".format(music_track_idx, n_tracks)
        )

    # ---- 全トラックをパース ----
    tempo_map   = []  # (abs_tick, tempo_us)
    note_events = []  # (abs_tick, 'on'/'off', note)

    for idx, buf in enumerate(track_bufs):
        evs = _parse_track(buf)
        for abs_tick, ev_type, ev_data in evs:
            if ev_type == 0xFF51:
                tempo_map.append((abs_tick, ev_data[0]))
            if idx == music_track_idx:
                if ev_type == 0x90:
                    note_events.append((abs_tick, 'on',  ev_data[0]))
                elif ev_type == 0x80:
                    note_events.append((abs_tick, 'off', ev_data[0]))

    # ---- テンポマップを tick 昇順に整列 ----
    tempo_map.sort(key=lambda x: x[0])
    # 先頭 tick=0 にテンポがなければデフォルト 120 BPM を挿入
    if not tempo_map or tempo_map[0][0] != 0:
        tempo_map.insert(0, (0, 500000))

    # ---- ノートイベントを tick 昇順に整列 ----
    note_events.sort(key=lambda x: x[0])

    # ---- tick → μs 変換（O(n_events + n_tempo) スイープ） ----
    # 両リストがソート済みなので、テンポマップを 1 パスで追いかける。
    result     = []
    ti         = 0              # tempo_map のインデックス
    cum_us     = 0              # tempo_map[ti][0] までの累積 μs
    seg_tick   = tempo_map[0][0]   # 現セグメント開始 tick
    seg_tempo  = tempo_map[0][1]   # 現セグメントのテンポ [μs/beat]

    for abs_tick, ev, note in note_events:
        # テンポチェンジを abs_tick まで適用
        while ti + 1 < len(tempo_map) and tempo_map[ti + 1][0] <= abs_tick:
            next_tick, next_tempo = tempo_map[ti + 1]
            cum_us   += (next_tick - seg_tick) * seg_tempo // ppqn
            seg_tick  = next_tick
            seg_tempo = next_tempo
            ti += 1

        time_us = cum_us + (abs_tick - seg_tick) * seg_tempo // ppqn
        result.append((time_us, ev, note))

    return result
