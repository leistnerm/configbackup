"""Bounded SMTP/HTTPS delivery. Credentials are referenced by environment name."""
from __future__ import annotations
import datetime as dt
from email.message import EmailMessage
import html
import json
import os
import smtplib
import ssl
import urllib.parse
import urllib.request


SECTIONS = ('alerts', 'capacity', 'freshness', 'schedules', 'recovery', 'changes')


def _cell(value, limit=250):
    return html.escape(str(value)[:limit])


def render_summary(summary, sections=SECTIONS, max_rows=30, max_bytes=100_000):
    """Static email-safe HTML and a text alternative; no scripts or external assets."""
    title = 'ConfigBackup operations summary'
    text = [title, str(summary.get('observed_at', '')), 'Status: ' + str(summary.get('status', 'unknown'))]
    pieces = ['<!doctype html><html><body style="font-family:Arial,sans-serif;color:#192737;max-width:900px">',
              '<h1 style="font-size:24px">' + title + '</h1>', '<p>' + _cell(summary.get('observed_at', '')) +
              ' · Status: <strong>' + _cell(summary.get('status', 'unknown')) + '</strong></p>']
    from sections import enabled
    for section in sections:
        if isinstance(sections,dict) and not enabled(section,sections): continue
        if section not in SECTIONS:
            raise ValueError('Unknown email summary section: ' + str(section))
        rows = summary.get(section) or []
        if not rows:
            continue
        pieces.append('<h2 style="font-size:18px">' + _cell(section.title()) + '</h2>')
        text.append('\n' + section.upper())
        # Whitelisted summary records contain facts only, never configuration text.
        columns = list(dict.fromkeys(k for row in rows[:max_rows] for k in row))[:8]
        pieces.append('<table cellpadding="6" cellspacing="0" style="border-collapse:collapse;font-size:12px;width:100%"><tr>' +
                      ''.join('<th style="text-align:left;background:#eaf1f8;border:1px solid #ccd7e2">' + _cell(k) + '</th>' for k in columns) + '</tr>')
        for row in rows[:max_rows]:
            pieces.append('<tr>' + ''.join('<td style="border:1px solid #ccd7e2">' + _cell(row.get(k, '')) + '</td>' for k in columns) + '</tr>')
            text.append('; '.join(k + ': ' + str(row.get(k, ''))[:250] for k in columns))
        pieces.append('</table>')
        if len(rows) > max_rows:
            pieces.append('<p>Showing ' + str(max_rows) + ' of ' + str(len(rows)) + ' rows. See the local dashboard.</p>')
    pieces.append('<p style="color:#596777">Unknown or stale data is not evidence of health. Detailed reports remain in the local dashboard.</p></body></html>')
    plain, rich = '\n'.join(text), ''.join(pieces)
    if len(rich.encode('utf-8')) > max_bytes:
        if max_rows > 1:
            return render_summary(summary, sections, max(1, max_rows//2), max_bytes)
        plain = title + '\nSummary exceeds the configured size limit. See the local dashboard.'
        rich = '<html><body><p>' + html.escape(plain) + '</p></body></html>'
    return plain, rich


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError('Notification redirects are disabled')


def environment(config, name, required=False):
    variable = config.get(name + '_env')
    value = os.environ.get(variable, '') if variable else ''
    if required and not value:
        raise ValueError('Required notification environment variable is unavailable: ' + name)
    return value


def send(channel, message):
    kind = channel['type']
    timeout = min(60, max(1, float(channel.get('timeout_seconds', 15))))
    if kind == 'smtp':
        mail = EmailMessage()
        mail['Subject'] = str(message.get('subject', 'ConfigBackup notification')).replace('\r', '').replace('\n', '')[:200]
        mail['From'] = channel['from']
        recipients = channel['to']
        if isinstance(recipients, str): recipients = [recipients]
        mail['To'] = ', '.join(recipients)
        mail['Date'] = dt.datetime.now(dt.timezone.utc)
        if message.get('summary'):
            plain, rich = render_summary(message['summary'], channel.get('sections', SECTIONS),
                                         int(channel.get('max_rows', 30)), int(channel.get('max_html_bytes', 100_000)))
        else:
            plain = str(message.get('text', ''))
            rich = '<html><body><pre style="white-space:pre-wrap">' + html.escape(plain) + '</pre></body></html>'
        if message.get('text') and message.get('summary'):
            plain = str(message['text']) + '\n\n' + plain
            rich = rich.replace('<body ', '<body ', 1).replace('<h1 ', '<pre style="white-space:pre-wrap">' + html.escape(str(message['text'])) + '</pre><h1 ', 1)
        mail.set_content(plain)
        if channel.get('html', True): mail.add_alternative(rich, subtype='html')
        host = channel['host']
        mode = channel.get('tls', 'starttls')
        if mode not in ('starttls', 'ssl', 'none'): raise ValueError('Unknown SMTP TLS mode')
        if mode == 'none' and not (channel.get('allow_insecure_localhost') and host in ('localhost', '127.0.0.1', '::1')):
            raise ValueError('SMTP without TLS is allowed only for explicit localhost tests')
        client = smtplib.SMTP_SSL if mode == 'ssl' else smtplib.SMTP
        options = {'timeout': timeout}
        if mode == 'ssl': options['context'] = ssl.create_default_context()
        with client(host, int(channel.get('port', 465 if mode == 'ssl' else 587)), **options) as smtp:
            smtp.ehlo()
            if mode == 'starttls': smtp.starttls(context=ssl.create_default_context()); smtp.ehlo()
            user = environment(channel, 'username')
            if user: smtp.login(user, environment(channel, 'password', required=True))
            refused = smtp.send_message(mail)
            if refused: raise RuntimeError('SMTP rejected one or more recipients')
        return
    if kind not in ('webhook', 'ntfy', 'heartbeat'):
        raise ValueError('Unknown notification channel type')
    url = environment(channel, 'url') or channel.get('url', '')
    parsed = urllib.parse.urlsplit(url)
    if parsed.username or parsed.password: raise ValueError('Use environment token fields, not URL credentials')
    if parsed.scheme != 'https' and not (parsed.scheme == 'http' and channel.get('allow_insecure_localhost') and parsed.hostname in ('localhost','127.0.0.1','::1')):
        raise ValueError('Notifications require HTTPS (except explicit localhost tests)')
    if kind == 'ntfy':
        body = str(message.get('text', '')).encode('utf-8')
        headers = {'Content-Type':'text/plain; charset=utf-8', 'Title':str(message.get('subject','ConfigBackup'))[:150].encode('ascii','replace').decode()}
    else:
        body = json.dumps(message, ensure_ascii=False).encode('utf-8')
        headers = {'Content-Type':'application/json'}
    token = environment(channel, 'token')
    if token: headers['Authorization'] = 'Bearer ' + token
    request = urllib.request.Request(url, data=body, headers=headers, method='POST')
    with urllib.request.build_opener(NoRedirect()).open(request, timeout=timeout) as response:
        if not 200 <= response.status < 300: raise RuntimeError('Notification endpoint returned non-success')
        response.read(1024)
