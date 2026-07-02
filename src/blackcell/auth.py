import json
import os
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

DEFAULT_GITHUB_HOST = "github.com"
DEFAULT_GITHUB_SCOPES = ("repo", "project", "read:org")
DEFAULT_MIN_TTL_SECONDS = 5 * 60 * 60
DEVICE_GRANT_TYPE = "urn:ietf:params:oauth:grant-type:device_code"
QR_VERSION = 3
QR_SIZE = 4 * QR_VERSION + 17
QR_DATA_CODEWORDS = 55
QR_ECC_CODEWORDS = 15
QR_MAX_BYTES = 53


class AuthError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class AuthSession:
    provider: str
    host: str
    access_token: str
    token_type: str = "bearer"
    scopes: tuple[str, ...] = ()
    created_at: str = ""
    expires_at: str | None = None
    refresh_token: str | None = None
    refresh_expires_at: str | None = None

    @classmethod
    def from_mapping(cls, data: dict[str, object]) -> AuthSession:
        scopes = data.get("scopes", ())
        if not isinstance(scopes, Sequence) or isinstance(scopes, str | bytes | bytearray):
            raise AuthError("auth cache scopes must be a sequence")
        return cls(
            provider=_string(data, "provider"),
            host=_string(data, "host"),
            access_token=_string(data, "access_token"),
            token_type=_string(data, "token_type", default="bearer"),
            scopes=tuple(_sequence_strings(scopes, "scopes")),
            created_at=_string(data, "created_at", default=""),
            expires_at=_optional_string(data, "expires_at"),
            refresh_token=_optional_string(data, "refresh_token"),
            refresh_expires_at=_optional_string(data, "refresh_expires_at"),
        )

    def to_mapping(self) -> dict[str, object]:
        data: dict[str, object] = {
            "provider": self.provider,
            "host": self.host,
            "access_token": self.access_token,
            "token_type": self.token_type,
            "scopes": list(self.scopes),
            "created_at": self.created_at,
        }
        if self.expires_at:
            data["expires_at"] = self.expires_at
        if self.refresh_token:
            data["refresh_token"] = self.refresh_token
        if self.refresh_expires_at:
            data["refresh_expires_at"] = self.refresh_expires_at
        return data

    def is_expired(self, *, now: datetime | None = None) -> bool:
        if self.expires_at is None:
            return False
        return _parse_timestamp(self.expires_at) <= (now or _utc_now())

    def sanitized(self, *, now: datetime | None = None) -> dict[str, object]:
        return {
            "authenticated": not self.is_expired(now=now),
            "provider": self.provider,
            "host": self.host,
            "token_type": self.token_type,
            "scopes": self.scopes,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "refresh_expires_at": self.refresh_expires_at,
        }


@dataclass(frozen=True, slots=True)
class DeviceCode:
    device_code: str
    user_code: str
    verification_uri: str
    expires_in: int
    interval: int
    verification_uri_complete: str | None = None

    @property
    def login_uri(self) -> str:
        return self.verification_uri_complete or self.verification_uri


@dataclass(frozen=True, slots=True)
class DeviceLoginResult:
    session: AuthSession
    path: Path

    def sanitized(self) -> dict[str, object]:
        payload = self.session.sanitized()
        payload["path"] = self.path
        return payload


def auth_cache_path() -> Path:
    if override := os.getenv("BLACKCELL_AUTH_FILE"):
        return Path(override).expanduser()
    if config_home := os.getenv("XDG_CONFIG_HOME"):
        root = Path(config_home).expanduser()
    else:
        root = Path.home() / ".config"
    return root / "blackcell" / "auth.json"


def load_auth_session(*, path: Path | None = None) -> AuthSession | None:
    auth_path = path or auth_cache_path()
    if not auth_path.exists():
        return None
    data = json.loads(auth_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise AuthError("auth cache root must be a JSON object")
    return AuthSession.from_mapping(data)


def load_valid_access_token(*, host: str = DEFAULT_GITHUB_HOST) -> str | None:
    try:
        session = load_auth_session()
    except (OSError, ValueError, AuthError):
        return None
    if session is None or session.provider != "github" or session.host != host:
        return None
    if session.is_expired():
        return None
    return session.access_token


def save_auth_session(session: AuthSession, *, path: Path | None = None) -> Path:
    auth_path = path or auth_cache_path()
    auth_path.parent.mkdir(parents=True, exist_ok=True)
    auth_path.write_text(
        json.dumps(session.to_mapping(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    auth_path.chmod(0o600)
    return auth_path


def delete_auth_session(*, path: Path | None = None) -> bool:
    auth_path = path or auth_cache_path()
    try:
        auth_path.unlink()
    except FileNotFoundError:
        return False
    return True


def _utc_now() -> datetime:
    return datetime.now(UTC)


def request_device_code(
    *,
    client_id: str,
    scopes: tuple[str, ...] = DEFAULT_GITHUB_SCOPES,
    client: httpx.Client | None = None,
) -> DeviceCode:
    http = client or httpx.Client(timeout=20)
    response = http.post(
        "https://github.com/login/device/code",
        headers={"Accept": "application/json"},
        data={"client_id": client_id, "scope": " ".join(scopes)},
    )
    response.raise_for_status()
    payload = response.json()
    if error := payload.get("error"):
        raise AuthError(str(payload.get("error_description") or error))
    return DeviceCode(
        device_code=_string(payload, "device_code"),
        user_code=_string(payload, "user_code"),
        verification_uri=_string(payload, "verification_uri"),
        expires_in=_int(payload, "expires_in"),
        interval=_int(payload, "interval"),
        verification_uri_complete=_optional_string(payload, "verification_uri_complete"),
    )


def poll_device_authorization(
    *,
    client_id: str,
    device_code: DeviceCode,
    scopes: tuple[str, ...] = DEFAULT_GITHUB_SCOPES,
    min_ttl_seconds: int = DEFAULT_MIN_TTL_SECONDS,
    client: httpx.Client | None = None,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], datetime] = _utc_now,
) -> AuthSession:
    http = client or httpx.Client(timeout=20)
    interval = device_code.interval
    deadline = time.monotonic() + device_code.expires_in
    sleep(interval)
    while True:
        response = http.post(
            "https://github.com/login/oauth/access_token",
            headers={"Accept": "application/json"},
            data={
                "client_id": client_id,
                "device_code": device_code.device_code,
                "grant_type": DEVICE_GRANT_TYPE,
            },
        )
        response.raise_for_status()
        payload = response.json()
        error = payload.get("error")
        if error == "authorization_pending":
            if time.monotonic() >= deadline:
                raise AuthError("device authorization expired")
            sleep(interval)
            continue
        if error == "slow_down":
            interval += 5
            sleep(interval)
            continue
        if error == "expired_token":
            raise AuthError("device authorization expired")
        if error == "access_denied":
            raise AuthError("device authorization was denied")
        if error:
            raise AuthError(str(payload.get("error_description") or error))

        access_token = _string(payload, "access_token")
        token_type = _string(payload, "token_type", default="bearer")
        session_scopes = _scopes_from_payload(payload) or scopes
        created_at = now()
        expires_in = payload.get("expires_in")
        expires_at = None
        if expires_in is not None:
            if not isinstance(expires_in, int):
                raise AuthError("GitHub token response expires_in must be an integer")
            if expires_in < min_ttl_seconds:
                raise AuthError(
                    "GitHub token lifetime is shorter than the required "
                    f"{min_ttl_seconds} seconds"
                )
            expires_at = _timestamp(created_at + timedelta(seconds=expires_in))

        refresh_expires_at = None
        refresh_expires_in = payload.get("refresh_token_expires_in")
        if refresh_expires_in is not None:
            if not isinstance(refresh_expires_in, int):
                raise AuthError("GitHub token response refresh_token_expires_in must be an integer")
            refresh_expires_at = _timestamp(created_at + timedelta(seconds=refresh_expires_in))

        return AuthSession(
            provider="github",
            host=DEFAULT_GITHUB_HOST,
            access_token=access_token,
            token_type=token_type,
            scopes=session_scopes,
            created_at=_timestamp(created_at),
            expires_at=expires_at,
            refresh_token=_optional_string(payload, "refresh_token"),
            refresh_expires_at=refresh_expires_at,
        )


def login_with_device_flow(
    *,
    client_id: str,
    scopes: tuple[str, ...] = DEFAULT_GITHUB_SCOPES,
    min_ttl_seconds: int = DEFAULT_MIN_TTL_SECONDS,
    client: httpx.Client | None = None,
    prompt: Callable[[DeviceCode], None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], datetime] = _utc_now,
) -> DeviceLoginResult:
    device_code = request_device_code(client_id=client_id, scopes=scopes, client=client)
    if prompt is not None:
        prompt(device_code)
    session = poll_device_authorization(
        client_id=client_id,
        device_code=device_code,
        scopes=scopes,
        min_ttl_seconds=min_ttl_seconds,
        client=client,
        sleep=sleep,
        now=now,
    )
    path = save_auth_session(session)
    return DeviceLoginResult(session=session, path=path)


def render_terminal_qr(value: str) -> str:
    data = value.encode("utf-8")
    if len(data) > QR_MAX_BYTES:
        raise AuthError(f"QR payload is too long; maximum is {QR_MAX_BYTES} bytes")

    modules, is_function = _blank_qr()
    codewords = _qr_data_codewords(data)
    codewords.extend(_reed_solomon_remainder(codewords, QR_ECC_CODEWORDS))
    _draw_qr_codewords(modules, is_function, codewords, mask=0)
    _draw_format_bits(modules, is_function, mask=0)
    return _terminal_qr(modules)


def _scopes_from_payload(payload: dict[str, object]) -> tuple[str, ...]:
    value = payload.get("scope")
    if not isinstance(value, str) or not value:
        return ()
    return tuple(scope for scope in value.replace(" ", ",").split(",") if scope)


def _string(data: dict[str, object], key: str, *, default: str | None = None) -> str:
    value = data.get(key, default)
    if not isinstance(value, str) or not value:
        raise AuthError(f"expected non-empty string for {key}")
    return value


def _optional_string(data: dict[str, object], key: str) -> str | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise AuthError(f"expected non-empty string for {key}")
    return value


def _int(data: dict[str, object], key: str) -> int:
    value = data.get(key)
    if not isinstance(value, int):
        raise AuthError(f"expected integer for {key}")
    return value


def _sequence_strings(values: Sequence[object], key: str) -> tuple[str, ...]:
    result: list[str] = []
    for index, value in enumerate(values):
        if not isinstance(value, str) or not value:
            raise AuthError(f"expected non-empty string for {key}[{index}]")
        result.append(value)
    return tuple(result)


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _blank_qr() -> tuple[list[list[bool]], list[list[bool]]]:
    modules = [[False for _ in range(QR_SIZE)] for _ in range(QR_SIZE)]
    is_function = [[False for _ in range(QR_SIZE)] for _ in range(QR_SIZE)]
    _draw_finder(modules, is_function, 0, 0)
    _draw_finder(modules, is_function, QR_SIZE - 7, 0)
    _draw_finder(modules, is_function, 0, QR_SIZE - 7)
    _draw_alignment(modules, is_function, 22, 22)
    _draw_timing(modules, is_function)
    _reserve_format_bits(modules, is_function)
    _set_function_module(modules, is_function, 8, QR_SIZE - 8, True)
    return modules, is_function


def _draw_finder(
    modules: list[list[bool]],
    is_function: list[list[bool]],
    left: int,
    top: int,
) -> None:
    for dy in range(-1, 8):
        for dx in range(-1, 8):
            x = left + dx
            y = top + dy
            if not _in_qr(x, y):
                continue
            black = (
                0 <= dx <= 6
                and 0 <= dy <= 6
                and (dx in {0, 6} or dy in {0, 6} or (2 <= dx <= 4 and 2 <= dy <= 4))
            )
            _set_function_module(modules, is_function, x, y, black)


def _draw_alignment(
    modules: list[list[bool]],
    is_function: list[list[bool]],
    center_x: int,
    center_y: int,
) -> None:
    for dy in range(-2, 3):
        for dx in range(-2, 3):
            distance = max(abs(dx), abs(dy))
            _set_function_module(
                modules,
                is_function,
                center_x + dx,
                center_y + dy,
                distance in {0, 2},
            )


def _draw_timing(modules: list[list[bool]], is_function: list[list[bool]]) -> None:
    for index in range(QR_SIZE):
        if not is_function[6][index]:
            _set_function_module(modules, is_function, index, 6, index % 2 == 0)
        if not is_function[index][6]:
            _set_function_module(modules, is_function, 6, index, index % 2 == 0)


def _reserve_format_bits(modules: list[list[bool]], is_function: list[list[bool]]) -> None:
    for index in range(0, 9):
        if index != 6:
            _set_function_module(modules, is_function, 8, index, False)
            _set_function_module(modules, is_function, index, 8, False)
    for index in range(8):
        _set_function_module(modules, is_function, QR_SIZE - 1 - index, 8, False)
    for index in range(8, 15):
        _set_function_module(modules, is_function, 8, QR_SIZE - 15 + index, False)


def _draw_format_bits(
    modules: list[list[bool]],
    is_function: list[list[bool]],
    *,
    mask: int,
) -> None:
    bits = _format_bits(mask=mask)
    for index in range(6):
        _set_function_module(modules, is_function, 8, index, _bit(bits, index))
    _set_function_module(modules, is_function, 8, 7, _bit(bits, 6))
    _set_function_module(modules, is_function, 8, 8, _bit(bits, 7))
    _set_function_module(modules, is_function, 7, 8, _bit(bits, 8))
    for index in range(9, 15):
        _set_function_module(modules, is_function, 14 - index, 8, _bit(bits, index))
    for index in range(8):
        _set_function_module(modules, is_function, QR_SIZE - 1 - index, 8, _bit(bits, index))
    for index in range(8, 15):
        _set_function_module(
            modules,
            is_function,
            8,
            QR_SIZE - 15 + index,
            _bit(bits, index),
        )
    _set_function_module(modules, is_function, 8, QR_SIZE - 8, True)


def _format_bits(*, mask: int) -> int:
    data = (0b01 << 3) | mask
    remainder = data << 10
    generator = 0b10100110111
    for shift in range(14, 9, -1):
        if (remainder >> shift) & 1:
            remainder ^= generator << (shift - 10)
    return ((data << 10) | remainder) ^ 0b101010000010010


def _qr_data_codewords(data: bytes) -> list[int]:
    bits: list[int] = []
    _append_bits(bits, 0b0100, 4)
    _append_bits(bits, len(data), 8)
    for byte in data:
        _append_bits(bits, byte, 8)
    capacity_bits = QR_DATA_CODEWORDS * 8
    _append_bits(bits, 0, min(4, capacity_bits - len(bits)))
    while len(bits) % 8:
        bits.append(0)

    codewords = [
        sum(bits[index + bit] << (7 - bit) for bit in range(8))
        for index in range(0, len(bits), 8)
    ]
    pad = 0xEC
    while len(codewords) < QR_DATA_CODEWORDS:
        codewords.append(pad)
        pad ^= 0xEC ^ 0x11
    return codewords


def _draw_qr_codewords(
    modules: list[list[bool]],
    is_function: list[list[bool]],
    codewords: list[int],
    *,
    mask: int,
) -> None:
    bits = [(codeword >> shift) & 1 == 1 for codeword in codewords for shift in range(7, -1, -1)]
    bit_index = 0
    upward = True
    x = QR_SIZE - 1
    while x > 0:
        if x == 6:
            x -= 1
        rows = range(QR_SIZE - 1, -1, -1) if upward else range(QR_SIZE)
        for y in rows:
            for dx in range(2):
                column = x - dx
                if is_function[y][column]:
                    continue
                bit = bit_index < len(bits) and bits[bit_index]
                bit_index += 1
                modules[y][column] = bit ^ _mask_bit(mask, column, y)
        upward = not upward
        x -= 2


def _reed_solomon_remainder(data: list[int], degree: int) -> list[int]:
    divisor = _reed_solomon_divisor(degree)
    result = [0] * degree
    for byte in data:
        factor = byte ^ result.pop(0)
        result.append(0)
        for index, coefficient in enumerate(divisor):
            result[index] ^= _gf_multiply(coefficient, factor)
    return result


def _reed_solomon_divisor(degree: int) -> list[int]:
    result = [0] * (degree - 1) + [1]
    root = 1
    for _ in range(degree):
        for index in range(degree):
            result[index] = _gf_multiply(result[index], root)
            if index + 1 < degree:
                result[index] ^= result[index + 1]
        root = _gf_multiply(root, 0x02)
    return result


def _gf_multiply(left: int, right: int) -> int:
    result = 0
    for _ in range(8):
        if right & 1:
            result ^= left
        right >>= 1
        carry = left & 0x80
        left = (left << 1) & 0xFF
        if carry:
            left ^= 0x1D
    return result


def _terminal_qr(modules: list[list[bool]]) -> str:
    quiet_zone = 2
    lines: list[str] = []
    for y in range(-quiet_zone, QR_SIZE + quiet_zone, 2):
        line: list[str] = []
        for x in range(-quiet_zone, QR_SIZE + quiet_zone):
            top = _qr_module(modules, x, y)
            bottom = _qr_module(modules, x, y + 1)
            if top and bottom:
                line.append("█")
            elif top:
                line.append("▀")
            elif bottom:
                line.append("▄")
            else:
                line.append(" ")
        lines.append("".join(line).rstrip())
    return "\n".join(lines)


def _qr_module(modules: list[list[bool]], x: int, y: int) -> bool:
    return _in_qr(x, y) and modules[y][x]


def _append_bits(bits: list[int], value: int, width: int) -> None:
    for shift in range(width - 1, -1, -1):
        bits.append((value >> shift) & 1)


def _set_function_module(
    modules: list[list[bool]],
    is_function: list[list[bool]],
    x: int,
    y: int,
    black: bool,
) -> None:
    modules[y][x] = black
    is_function[y][x] = True


def _mask_bit(mask: int, x: int, y: int) -> bool:
    if mask != 0:
        raise AuthError(f"unsupported QR mask {mask}")
    return (x + y) % 2 == 0


def _bit(value: int, index: int) -> bool:
    return ((value >> index) & 1) != 0


def _in_qr(x: int, y: int) -> bool:
    return 0 <= x < QR_SIZE and 0 <= y < QR_SIZE
