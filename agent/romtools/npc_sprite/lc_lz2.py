"""LC_LZ2 codec for A Link to the Past graphics packets.

ALTTP stores most graphics (backgrounds, sprite/NPC sheets, menu gfx) compressed
with the LC_LZ2 format (the same family Super Mario World uses). The decompressed
sheets are DMA'd into VRAM at runtime.

Stream format (per SnesLab / MushROMs docs):
    Each chunk starts with a header byte:  CCCLLLLL
        CCC   = command (0-4 used)
        LLLLL = length, output count is (L + 1)
    Commands:
        000 Direct Copy     copy the next (L+1) literal bytes
        001 Byte Fill       repeat the next 1 byte (L+1) times
        010 Word Fill       alternate the next 2 bytes for (L+1) bytes
        011 Increasing Fill next byte, written (L+1) times, +1 each write
        100 Repeat          copy (L+1) bytes from an absolute output offset
                            given by the next 2 bytes, LITTLE-ENDIAN
                            (generic LC_LZ2 docs say big-endian, but ALTTP's
                            actual Decompress routine at $00E865 reads it
                            little-endian -- verified against jpdasm)
    Long length (command bits == 111): the header is two bytes:
        111CCCLL LLLLLLLL  -> CCC is the real command, 10-bit length (L+1, max 1024)
    The byte 0xFF terminates the stream.

This module is ROM-agnostic: pass it raw bytes.
"""

# command numbers
CMD_DIRECT_COPY = 0
CMD_BYTE_FILL = 1
CMD_WORD_FILL = 2
CMD_INC_FILL = 3
CMD_REPEAT = 4
CMD_LONG = 7


def decompress(data, offset=0):
    """Decompress an LC_LZ2 stream starting at ``offset`` in ``data``.

    Returns ``(decompressed_bytes, compressed_length)`` where compressed_length
    includes the terminating 0xFF, so ``offset + compressed_length`` is the byte
    just past the stream.
    """
    out = bytearray()
    pos = offset
    n = len(data)

    while True:
        if pos >= n:
            raise ValueError(f"LC_LZ2 stream ran off the end at {pos:#x} (no 0xFF terminator)")
        header = data[pos]
        pos += 1

        if header == 0xFF:
            break

        command = header >> 5
        if command == CMD_LONG:
            # extended header: 111CCCLL LLLLLLLL
            command = (header >> 2) & 0x07
            if pos >= n:
                raise ValueError("truncated long-length header")
            length = ((header & 0x03) << 8) | data[pos]
            pos += 1
        else:
            length = header & 0x1F
        length += 1  # encoded value is count-1

        if command == CMD_DIRECT_COPY:
            out += data[pos:pos + length]
            pos += length
        elif command == CMD_BYTE_FILL:
            out += bytes([data[pos]]) * length
            pos += 1
        elif command == CMD_WORD_FILL:
            pair = data[pos:pos + 2]
            pos += 2
            for i in range(length):
                out.append(pair[i & 1])
        elif command == CMD_INC_FILL:
            value = data[pos]
            pos += 1
            for _ in range(length):
                out.append(value & 0xFF)
                value += 1
        elif command == CMD_REPEAT:
            src = data[pos] | (data[pos + 1] << 8)  # little-endian absolute output offset (ALTTP)
            pos += 2
            for i in range(length):
                out.append(out[src + i])
        else:
            raise ValueError(f"unsupported LC_LZ2 command {command} (header {header:#04x}) at {pos-1:#x}")

    return bytes(out), pos - offset


def _emit_header(out, command, length):
    """Append a chunk header for ``length`` output bytes (length is the real count)."""
    count = length - 1
    if count < 0x20:
        out.append((command << 5) | count)
    else:
        # long form: 111CCCLL LLLLLLLL
        out.append(0xE0 | (command << 2) | ((count >> 8) & 0x03))
        out.append(count & 0xFF)


def compress(data, use_runs=True):
    """Compress ``data`` into a valid (and reasonably small) LC_LZ2 stream.

    Greedy encoder using Byte Fill (cmd 1), Word Fill (cmd 2) and Repeat (cmd 4,
    an LZ back-reference into the output produced so far), falling back to Direct
    Copy literals. Always lossless. Max chunk length is 1024 (long-header limit).
    Output offsets for Repeat must fit in 16 bits, which holds for ALTTP sheets.
    """
    out = bytearray()
    n = len(data)
    i = 0
    literal_start = i
    MAXLEN = 1024

    def flush_literals(end):
        j = literal_start
        while j < end:
            chunk = min(end - j, MAXLEN)
            _emit_header(out, CMD_DIRECT_COPY, chunk)
            out.extend(data[j:j + chunk])
            j += chunk

    while i < n:
        # option A: byte fill (run of identical bytes)
        run = 1
        while i + run < n and data[i + run] == data[i] and run < MAXLEN:
            run += 1

        # option B: word fill (alternating 2-byte pattern)
        wrun = 0
        if i + 1 < n:
            a, b = data[i], data[i + 1]
            wrun = 2
            while i + wrun < n and data[i + wrun] == (a if wrun % 2 == 0 else b) and wrun < MAXLEN:
                wrun += 1

        # option C: repeat — longest match of data[i:] within data[0:i]
        # (the output buffer so far equals data[:i]); offset must be < 0x10000
        best_len, best_src = 0, 0
        if i > 0 and i < 0x10000:
            start = max(0, i - 0xFFFF)
            # scan candidate sources; cheap brute force (sheets are ~0x600)
            for src in range(start, i):
                if data[src] != data[i]:
                    continue
                L = 1
                while (i + L < n and L < MAXLEN and data[src + L] == data[i + L]):
                    L += 1
                if L > best_len:
                    best_len, best_src = L, src
                    if L >= MAXLEN:
                        break

        # choose the option that emits the most bytes for the least cost
        use = None
        if use_runs and run >= 3 and run >= best_len and run >= wrun:
            use = ("byte", run)
        elif best_len >= 4 and best_len >= wrun:
            use = ("repeat", best_len)
        elif wrun >= 4:
            use = ("word", wrun)
        elif use_runs and run >= 3:
            use = ("byte", run)

        if use:
            flush_literals(i)
            kind, length = use
            if kind == "byte":
                _emit_header(out, CMD_BYTE_FILL, length)
                out.append(data[i])
            elif kind == "word":
                _emit_header(out, CMD_WORD_FILL, length)
                out.append(data[i]); out.append(data[i + 1])
            else:  # repeat (little-endian absolute output offset)
                _emit_header(out, CMD_REPEAT, length)
                out.append(best_src & 0xFF); out.append((best_src >> 8) & 0xFF)
            i += length
            literal_start = i
        else:
            i += 1

    flush_literals(n)
    out.append(0xFF)
    return bytes(out)
