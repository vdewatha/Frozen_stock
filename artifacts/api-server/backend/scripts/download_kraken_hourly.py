"""Retrieve only XBTUSD_60.csv from Kraken's official large ZIP using HTTP ranges."""
import argparse
import io
from pathlib import Path
import re
import subprocess
import tempfile
import zipfile

URL = "https://drive.usercontent.google.com/download?id=1ptNqWYidLkhb2VAKuLCxmp2OXEfGO-AP&export=download&confirm=t"


def request(url, *, limit, byte_range=None):
    """Bound transfers even when a server ignores Range and sends the full ZIP."""
    with tempfile.TemporaryDirectory(prefix="kraken-range-") as temporary:
        headers = Path(temporary) / "headers"
        args = ["curl", "--http1.1", "--silent", "--show-error", "--fail", "--location",
                "--proto", "=https", "--proto-redir", "=https", "--max-redirs", "3",
                "--max-time", "90", "--connect-timeout", "20", "--max-filesize", str(limit),
                "--dump-header", str(headers)]
        if byte_range:
            args += ["--range", byte_range]
            # Distinct cache keys prevent intermediaries reusing one partial
            # response for a different Range on the same download URL.
            url += "&range_key=" + byte_range
        result = subprocess.run(args + [url], capture_output=True, timeout=100)
        if result.returncode or len(result.stdout) > limit:
            raise ValueError(f"Bounded HTTPS download failed (curl {result.returncode}, range {byte_range})")
        blocks = headers.read_text().replace("\r\n", "\n").strip().split("\n\n")
        response = blocks[-1].splitlines()
        status = int(response[0].split()[1])
        fields = dict(line.split(":", 1) for line in response[1:] if ":" in line)
        return status, {key.lower(): value.strip() for key, value in fields.items()}, result.stdout


class RemoteZip(io.RawIOBase):
    def __init__(self, url):
        self.url, self.position, self.transferred = url, 0, 0
        status, headers, data = request(url, limit=1, byte_range="0-0")
        match = re.fullmatch(r"bytes 0-0/(\d+)", headers.get("content-range", ""))
        if status != 206 or not match or len(data) != 1:
            raise ValueError("Server did not provide bounded byte ranges")
        self.size = int(match[1])
        self.transferred = 1
        self.tail_start = max(0, self.size - 65536)
        self.tail = None

    def seekable(self): return True
    def tell(self): return self.position
    def seek(self, offset, whence=0):
        position = offset if whence == 0 else self.position + offset if whence == 1 else self.size + offset
        if not 0 <= position <= self.size: raise ValueError("Invalid archive offset")
        self.position = position
        return position

    def read(self, size=-1):
        size = self.size - self.position if size < 0 else min(size, self.size - self.position)
        if size == 0: return b""
        if self.position >= self.tail_start:
            if self.tail is None:
                if self.transferred + self.size - self.tail_start > 127 * 1024 * 1024:
                    raise ValueError("Selective download size budget exceeded")
                status, headers, data = request(self.url, limit=self.size - self.tail_start,
                    byte_range=f"{self.tail_start}-{self.size - 1}")
                if status != 206 or headers.get("content-range") != f"bytes {self.tail_start}-{self.size - 1}/{self.size}" or len(data) != self.size - self.tail_start:
                    raise ValueError("Tail cache range mismatch")
                self.tail = data
                self.transferred += len(data)
            data = self.tail[self.position - self.tail_start:self.position - self.tail_start + size]
            self.position += len(data)
            return data
        if size > 64 * 1024 * 1024 or self.transferred + size > 127 * 1024 * 1024:
            raise ValueError("Selective download size budget exceeded")
        end = self.position + size - 1
        status, headers, data = request(self.url, limit=size, byte_range=f"{self.position}-{end}")
        if status != 206 or headers.get("content-range") != f"bytes {self.position}-{end}/{self.size}":
            raise ValueError("Range response mismatch")
        if len(data) != size: raise ValueError("Truncated or oversized range")
        self.position += size
        self.transferred += size
        return data


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--confirmation-uuid", help="Previously observed official confirmation-form UUID")
    args = parser.parse_args()
    status, _, data = request("https://drive.google.com/uc?export=download&id=1ptNqWYidLkhb2VAKuLCxmp2OXEfGO-AP", limit=100000)
    if status != 200: raise ValueError("Confirmation page failed")
    page = data.decode("utf-8")
    match = re.search(r'name="uuid" value="([a-zA-Z0-9-]+)"', page)
    if not match: raise ValueError("Expected official archive confirmation form")
    confirmation = args.confirmation_uuid or match[1]
    if not re.fullmatch(r"[a-zA-Z0-9-]{20,50}", confirmation): raise ValueError("Invalid confirmation UUID")
    remote = RemoteZip(URL + "&uuid=" + confirmation)
    with zipfile.ZipFile(remote) as archive:
        candidates = [i for i in archive.infolist() if Path(i.filename).name == "XBTUSD_60.csv"]
        if len(candidates) != 1:
            raise ValueError(f"Expected one BTC/USD hourly member, found {len(candidates)}")
        member = candidates[0]
        print(f"Archive bytes: {remote.size}; member: {member.filename}; CSV bytes: {member.file_size}")
        if args.output is None: return
        if member.file_size > 64 * 1024 * 1024: raise ValueError("CSV size limit")
        data = archive.read(member)  # ZIP CRC verified; never executes or extracts arbitrary paths.
        with args.output.open("xb") as stream: stream.write(data)
        print(f"Saved hourly CSV; transferred {remote.transferred} bytes, not the full archive")


if __name__ == "__main__": main()
