"""Conservative pre-commit credential gate. Findings never include secret values."""
import hashlib
import io
import re
import zipfile

RULES = {
    'sql-password-hash': rb'(?i)\bpassword\s*=\s*0x[0-9a-f]{16,}',
    'postgres-password-hash': rb'(?i)(?:SCRAM-SHA-256\$[0-9]+:|\bmd5[0-9a-f]{32}\b)',
    # SQL Unicode literals (N'...') and escaped quotes must not bypass the gate.
    'sql-password-literal': rb"(?i)\b(?:password|pwd)\s*=\s*N?'((?:[^']|''){4,})'",
    'postgres-option-literal': rb"(?i)\b(?:password|sslpassword|ldapbindpasswd|radiussecrets?)\"?\s+'((?:[^']|''){1,})'",
    'postgres-auth-option': rb'''(?i)\b(?:ldapbindpasswd|radiussecret|radiussecrets)\s*[=:]\s*["']?([^\s;,'"<>}{]{1,})''',
    'private-key': rb'-----BEGIN (?:RSA |EC |OPENSSH |DSA |ENCRYPTED )?PRIVATE KEY-----',
    'github-token': rb'\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})',
    'aws-access-key': rb'\bAKIA[A-Z0-9]{16}\b',
    'credential-assignment': rb'''(?i)\b(?:password|passwd|pwd|client_secret|api_key|access_token)\s*[=:]\s*["']?([^\s;,'"<>]{4,})''',
    'xml-credential': rb'(?i)<(?:password|passwd|client_secret|api_key)>[^<\s]{4,}</',
    'credential-uri': rb'(?i)\b[a-z][a-z0-9+.-]*://[^\s/:@]+:([^\s/@]+)@',
}


def scan_bytes(data, name, allow_sha256=(), depth=0, *, allow_sensitive=False):
    if hashlib.sha256(data).hexdigest() in allow_sha256:
        return []
    if depth > 3 or len(data) > 100 * 1024 * 1024:
        return [name + ': scan limit exceeded']
    findings = []
    if data.startswith((b'\xff\xfe', b'\xfe\xff')):
        try:data = data.decode('utf-16').encode('utf-8')
        except UnicodeError:return [name + ': cannot decode UTF-16 content']
    for rule, expression in RULES.items():
        for match in re.finditer(expression, data):
            value = match.group(1) if match.lastindex else b''
            if value.lower() in (b'redacted', b'null', b'none', b'false', b'true') or value.startswith((b'${', b'$(', b'@')):
                continue
            if not allow_sensitive:findings.append(name + ': ' + rule)
            break
    if data.startswith(b'PK\x03\x04'):
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                if sum(x.file_size for x in archive.infolist()) > 100 * 1024 * 1024:
                    return findings + [name + ': expanded archive exceeds scan limit']
                for item in archive.infolist():
                    if not item.is_dir():
                        findings += scan_bytes(archive.read(item), name + '!' + item.filename, allow_sha256, depth + 1, allow_sensitive=allow_sensitive)
        except (OSError, ValueError, RuntimeError, zipfile.BadZipFile):
            findings.append(name + ': archive could not be scanned')
    return findings
