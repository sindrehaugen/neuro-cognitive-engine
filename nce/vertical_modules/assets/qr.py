"""
nce/vertical_modules/assets/qr.py
==================================
Asset QR code generator and room-centric asset register cores (Wave A-4).

Implements:
  1. Pure-Python QR Code Model 2 generator (Versions 1-6, EC Level L, Byte Mode).
     Produces standard 2D bit matrices and vector SVG without third-party dependencies.
  2. ``do_generate_asset_qr`` — generates deep-linking QR metadata and scalable SVG
     for an asset in the register.
  3. ``do_get_room_register`` — queries the fast relational register (``assets`` table)
     for all devices within a given room (``functional_location_id``), providing
     the unredacted operator view.
"""

from __future__ import annotations

import logging
from typing import Any, Final
from uuid import UUID

from nce.db_utils import scoped_pg_session
from nce.mcp_args import require_namespace_id

log = logging.getLogger("nce.vertical_modules.assets.qr")

# =============================================================================
# Pure-Python QR Code Model 2 Encoder (ISO/IEC 18004)
# =============================================================================

# GF(256) with primitive polynomial 0x11D (285)
_EXP: Final[list[int]] = [0] * 512
_LOG: Final[list[int]] = [0] * 256

_val = 1
for _i in range(255):
    _EXP[_i] = _val
    _EXP[_i + 255] = _val
    _LOG[_val] = _i
    _val = (_val << 1) ^ 285 if (_val & 0x80) else (_val << 1)


def _gf_mul(x: int, y: int) -> int:
    """Multiply two numbers in Galois Field GF(256)."""
    return 0 if x == 0 or y == 0 else _EXP[_LOG[x] + _LOG[y]]


def _rs_encode(msg: bytes, n_ecc: int) -> bytes:
    """Compute Reed-Solomon error correction codewords for message bytes."""
    gen = [1]
    for i in range(n_ecc):
        root = _EXP[i]
        next_gen = [0] * (len(gen) + 1)
        for j, c in enumerate(gen):
            next_gen[j] ^= c
            next_gen[j + 1] ^= _gf_mul(c, root)
        gen = next_gen

    res = list(msg) + [0] * n_ecc
    for i in range(len(msg)):
        coef = res[i]
        if coef != 0:
            for j in range(len(gen)):
                res[i + j] ^= _gf_mul(gen[j], coef)
    return bytes(res[len(msg) :])


# Version configurations: (size, total_cw, data_cw, ec_cw, align_coords)
_QR_VERSIONS: Final[dict[int, dict[str, Any]]] = {
    1: {"size": 21, "total_cw": 26, "data_cw": 19, "ec_cw": 7, "align": []},
    2: {"size": 25, "total_cw": 44, "data_cw": 34, "ec_cw": 10, "align": [6, 18]},
    3: {"size": 29, "total_cw": 70, "data_cw": 55, "ec_cw": 15, "align": [6, 22]},
    4: {"size": 33, "total_cw": 100, "data_cw": 80, "ec_cw": 20, "align": [6, 26]},
    5: {"size": 37, "total_cw": 134, "data_cw": 108, "ec_cw": 26, "align": [6, 30]},
    6: {"size": 41, "total_cw": 172, "data_cw": 136, "ec_cw": 36, "align": [6, 34]},
}

# Precomputed format info for EC level L, Mask 0: 0x77C4 (15 bits)
_FORMAT_INFO_L_MASK0: Final[str] = "111011111000100"


def generate_qr_matrix(data: str) -> list[list[int]]:
    """Generate a standard QR Code Model 2 bit matrix for *data* (Byte mode, EC Level L)."""
    raw_bytes = data.encode("utf-8")
    byte_len = len(raw_bytes)

    # 1. Select smallest fitting version
    version = None
    v_info = None
    for v, info in sorted(_QR_VERSIONS.items()):
        if byte_len + 2 <= info["data_cw"]:
            version = v
            v_info = info
            break
    if version is None or v_info is None:
        raise ValueError(f"Payload too large for QR generator ({byte_len} bytes; max 134 bytes)")

    size: int = v_info["size"]
    data_cw: int = v_info["data_cw"]
    ec_cw: int = v_info["ec_cw"]

    # 2. Build data bitstream (Byte mode: 0100, 8-bit length)
    bitstream: list[int] = []

    def _append_bits(val: int, count: int) -> None:
        for b in range(count - 1, -1, -1):
            bitstream.append((val >> b) & 1)

    _append_bits(0b0100, 4)  # Mode indicator: Byte mode
    _append_bits(byte_len, 8)  # Character count
    for byte in raw_bytes:
        _append_bits(byte, 8)

    # Terminator (up to 4 bits)
    remaining_capacity = data_cw * 8 - len(bitstream)
    terminator_len = min(4, max(0, remaining_capacity))
    bitstream.extend([0] * terminator_len)

    # Pad to byte boundary
    while len(bitstream) % 8 != 0:
        bitstream.append(0)

    # Convert to bytes and pad to data_cw
    codewords = bytearray()
    for i in range(0, len(bitstream), 8):
        byte_val = 0
        for bit in bitstream[i : i + 8]:
            byte_val = (byte_val << 1) | bit
        codewords.append(byte_val)

    pad_bytes = [0xEC, 0x11]
    pad_idx = 0
    while len(codewords) < data_cw:
        codewords.append(pad_bytes[pad_idx])
        pad_idx ^= 1

    # 3. Compute Reed-Solomon Error Correction
    ec_codewords = _rs_encode(bytes(codewords), ec_cw)
    total_codewords = bytes(codewords) + ec_codewords

    # 4. Initialize matrix & reservation grid
    matrix: list[list[int]] = [[0] * size for _ in range(size)]
    reserved: list[list[bool]] = [[False] * size for _ in range(size)]

    def _mark_reserved(r: int, c: int, val: int) -> None:
        if 0 <= r < size and 0 <= c < size:
            matrix[r][c] = val
            reserved[r][c] = True

    # 5. Place Finder Patterns (7x7 + 1 separator module)
    def _place_finder(start_r: int, start_c: int) -> None:
        for r in range(-1, 8):
            for c in range(-1, 8):
                gr, gc = start_r + r, start_c + c
                if 0 <= gr < size and 0 <= gc < size:
                    if 0 <= r <= 6 and 0 <= c <= 6:
                        if r in (0, 6) or c in (0, 6) or (2 <= r <= 4 and 2 <= c <= 4):
                            _mark_reserved(gr, gc, 1)
                        else:
                            _mark_reserved(gr, gc, 0)
                    else:
                        _mark_reserved(gr, gc, 0)

    _place_finder(0, 0)
    _place_finder(0, size - 7)
    _place_finder(size - 7, 0)

    # 6. Place Alignment Patterns (for version >= 2)
    align_coords = v_info["align"]
    for ar in align_coords:
        for ac in align_coords:
            # Skip if overlapping finders
            if (ar < 9 and ac < 9) or (ar < 9 and ac > size - 9) or (ar > size - 9 and ac < 9):
                continue
            for dr in range(-2, 3):
                for dc in range(-2, 3):
                    gr, gc = ar + dr, ac + dc
                    if max(abs(dr), abs(dc)) in (0, 2):
                        _mark_reserved(gr, gc, 1)
                    else:
                        _mark_reserved(gr, gc, 0)

    # 7. Timing Patterns
    for i in range(8, size - 8):
        if not reserved[6][i]:
            _mark_reserved(6, i, 1 if i % 2 == 0 else 0)
        if not reserved[i][6]:
            _mark_reserved(i, 6, 1 if i % 2 == 0 else 0)

    # 8. Dark Module at (size - 8, 8)
    _mark_reserved(size - 8, 8, 1)

    # 9. Reserve Format Information Modules
    for i in range(9):
        if 0 <= i < size:
            reserved[8][i] = True
            reserved[i][8] = True
    for i in range(size - 8, size):
        if 0 <= i < size:
            reserved[8][i] = True
    for i in range(size - 7, size):
        if 0 <= i < size:
            reserved[i][8] = True

    # 10. Place Data Codewords (zig-zag right-to-left, skipping col 6)
    all_bits: list[int] = []
    for cw in total_codewords:
        for b in range(7, -1, -1):
            all_bits.append((cw >> b) & 1)

    bit_idx = 0
    c = size - 1
    upward = True
    while c > 0:
        if c == 6:  # Skip vertical timing pattern
            c -= 1
        row_range = range(size - 1, -1, -1) if upward else range(size)
        for r in row_range:
            for dc in (0, -1):
                col = c + dc
                if not reserved[r][col]:
                    bit = all_bits[bit_idx] if bit_idx < len(all_bits) else 0
                    bit_idx += 1
                    # Apply Mask 0: (row + col) % 2 == 0
                    if (r + col) % 2 == 0:
                        bit ^= 1
                    matrix[r][col] = bit
        upward = not upward
        c -= 2

    # 11. Place Format Information Bits (Format Info Level L, Mask 0)
    fmt = _FORMAT_INFO_L_MASK0
    # Top-left horizontal & vertical
    matrix[8][0] = int(fmt[0])
    matrix[8][1] = int(fmt[1])
    matrix[8][2] = int(fmt[2])
    matrix[8][3] = int(fmt[3])
    matrix[8][4] = int(fmt[4])
    matrix[8][5] = int(fmt[5])
    matrix[8][7] = int(fmt[6])
    matrix[8][8] = int(fmt[7])
    matrix[7][8] = int(fmt[8])
    matrix[5][8] = int(fmt[9])
    matrix[4][8] = int(fmt[10])
    matrix[3][8] = int(fmt[11])
    matrix[2][8] = int(fmt[12])
    matrix[1][8] = int(fmt[13])
    matrix[0][8] = int(fmt[14])

    # Bottom-left vertical & top-right horizontal
    for i in range(7):
        matrix[size - 1 - i][8] = int(fmt[i])
    for i in range(8):
        matrix[8][size - 8 + i] = int(fmt[7 + i])

    return matrix


def render_qr_svg(matrix: list[list[int]], box_size: int = 8, border: int = 4) -> str:
    """Render a 2D QR matrix into a clean, standalone SVG XML string."""
    size = len(matrix)
    dim = (size + 2 * border) * box_size
    rects: list[str] = []
    for r in range(size):
        for c in range(size):
            if matrix[r][c] == 1:
                x = (c + border) * box_size
                y = (r + border) * box_size
                rects.append(
                    f'<rect x="{x}" y="{y}" width="{box_size}" height="{box_size}" fill="#000000"/>'
                )

    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {dim} {dim}" '
        f'width="{dim}" height="{dim}">'
        f'<rect width="100%" height="100%" fill="#ffffff"/>'
        f'{"".join(rects)}'
        f"</svg>"
    )


# =============================================================================
# Domain Cores: do_generate_asset_qr & do_get_room_register
# =============================================================================


def _require_asset_id(params: dict[str, Any]) -> str:
    """Extract and validate asset_id UUID."""
    raw = params.get("asset_id") or params.get("id")
    if not raw:
        raise ValueError("'asset_id' is required")
    try:
        return str(UUID(str(raw)))
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError(f"invalid asset_id: {exc}") from exc


async def do_generate_asset_qr(engine: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Generate an asset QR code and deep-link payload for the room register (Wave A-4).

    Parameters
    ----------
    params:
        ``namespace_id`` (str, required): Active tenant UUID.
        ``asset_id``     (str, required): The asset UUID.
        ``base_url``     (str, optional): Custom portal base URL for deep-links.

    Returns
    -------
    dict
        Structured QR metadata, deep-link URL, and vector SVG.
    """
    namespace_id = require_namespace_id(params)
    asset_id = _require_asset_id(params)
    base_url = str(params.get("base_url") or "").rstrip("/")

    async with scoped_pg_session(engine.pg_pool, namespace_id) as conn:
        row = await conn.fetchrow(
            """
            SELECT id, bom_line_id, serial, functional_location_id, lifecycle_state
            FROM assets
            WHERE namespace_id = $1::uuid AND id = $2::uuid
            """,
            namespace_id,
            asset_id,
        )

    if row is None:
        return {
            "ok": False,
            "error": f"Asset '{asset_id}' not found",
            "asset_id": asset_id,
        }

    serial = str(row["serial"] or "")
    room_id = str(row["functional_location_id"] or "")
    bom_line_id = str(row["bom_line_id"] or "")
    lifecycle_state = str(row["lifecycle_state"] or "")

    # Construct deep link URL
    prefix = base_url if base_url else ""
    room_part = f"/rooms/{room_id}" if room_id else "/rooms/unassigned"
    deep_link_url = f"{prefix}{room_part}/assets/{asset_id}"

    # QR payload encoded in the matrix: standard URL or URI format
    qr_payload = deep_link_url if base_url else f"nce://assets/{asset_id}?room={room_id}"

    # Generate matrix and SVG
    matrix = generate_qr_matrix(qr_payload)
    svg = render_qr_svg(matrix, box_size=8, border=4)

    return {
        "ok": True,
        "asset_id": asset_id,
        "serial": serial,
        "bom_line_id": bom_line_id,
        "functional_location_id": room_id,
        "lifecycle_state": lifecycle_state,
        "deep_link_url": deep_link_url,
        "qr_payload": qr_payload,
        "qr_svg": svg,
        "qr_matrix": matrix,
        "dimensions": {
            "version": (len(matrix) - 17) // 4,
            "modules": len(matrix),
            "box_size": 8,
            "border": 4,
            "pixel_size": (len(matrix) + 8) * 8,
        },
    }


async def do_get_room_register(engine: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Retrieve the room-centric asset register (Wave A-4).

    Parameters
    ----------
    params:
        ``namespace_id``           (str, required): Active tenant UUID.
        ``functional_location_id`` (str, required): Target room identifier (or ``room_id``).

    Returns
    -------
    dict
        ``{"ok": True, "room_id": str, "total_assets": int, "assets": list}``
    """
    namespace_id = require_namespace_id(params)
    room_id = str(params.get("functional_location_id") or params.get("room_id") or "").strip()
    if not room_id:
        raise ValueError("'functional_location_id' or 'room_id' is required")

    async with scoped_pg_session(engine.pg_pool, namespace_id) as conn:
        rows = await conn.fetch(
            """
            SELECT id, bom_line_id, serial, functional_location_id, lifecycle_state,
                   change_origin, created_at, updated_at
            FROM assets
            WHERE namespace_id = $1::uuid AND functional_location_id = $2
            ORDER BY created_at ASC
            """,
            namespace_id,
            room_id,
        )

    assets: list[dict[str, Any]] = []
    for r in rows:
        aid = str(r["id"])
        assets.append(
            {
                "asset_id": aid,
                "bom_line_id": r["bom_line_id"],
                "serial": r["serial"],
                "functional_location_id": r["functional_location_id"],
                "lifecycle_state": r["lifecycle_state"],
                "change_origin": r["change_origin"],
                "deep_link_url": f"/rooms/{room_id}/assets/{aid}",
                "qr_endpoint": f"/api/assets/{aid}/qr",
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
                "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
            }
        )

    return {
        "ok": True,
        "room_id": room_id,
        "namespace_id": str(namespace_id),
        "total_assets": len(assets),
        "assets": assets,
    }
