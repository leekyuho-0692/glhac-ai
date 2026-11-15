/* Shared delivery results for the notification bell and operations history. */
function NTF_statusBadge(status){
  const known = ['unsent', 'sending', 'sent', 'partial', 'skipped', 'failed'];
  const state = known.includes(status) ? status : 'unknown';
  return `<span class="badge ntf-${state}" data-notify-status="${state}" data-noi18n>${esc(t('notify.status.' + state))}</span>`;
}

function NTF_channelName(channel){
  return ['inapp', 'email', 'sms', 'whatsapp', 'kakao', 'webhook'].includes(channel)
    ? t('notify.channel.' + channel) : channel;
}

function NTF_reasonText(reason, status){
  const code = String(reason || '');
  const known = ['no_credentials', 'no_contact', 'not_implemented', 'consent_skipped',
    'case_closed', 'no_result', 'smtp_tls_failed', 'send_error'];
  if(known.includes(code)) return t('notify.reason.' + code);
  if(code === 'error') return t('notify.reason.send_error');
  if(/^smtp_error(?::|$)/.test(code)) return t('notify.reason.smtp_error');
  if(/^twilio_(?:error|\d+)(?::|$)/.test(code)) return t('notify.reason.twilio_error');
  if(/^webhook_(?:error|\d+)(?::|$)/.test(code)) return t('notify.reason.webhook_error');
  return t(status === 'failed' ? 'notify.reason.send_error' : 'notify.reason.unknown');
}

function NTF_channelResults(notification, withReasons = true){
  const states = notification.channel_status || {};
  const channels = [...new Set([...(notification.channels || []), ...Object.keys(states)])];
  return channels.map(channel => {
    const result = states[channel] || {};
    const status = result.status || (['unsent', 'sending'].includes(notification.status) ? notification.status : 'unknown');
    const reason = withReasons && ['skipped', 'failed'].includes(status)
      ? ` <span class="muted">(${esc(NTF_reasonText(result.reason, status))})</span>` : '';
    return `<div class="ntf-channel-result" data-noi18n>${esc(NTF_channelName(channel))} ${NTF_statusBadge(status)}${reason}</div>`;
  }).join('');
}

function NTF_deliveryReasons(notification){
  const reasons = Object.entries(notification.channel_status || {})
    .filter(([, result]) => ['skipped', 'failed'].includes(result.status))
    .map(([channel, result]) => `${NTF_channelName(channel)}: ${NTF_reasonText(result.reason, result.status)}`);
  if(!reasons.length && notification.delivery_reason){
    reasons.push(NTF_reasonText(notification.delivery_reason, notification.status));
  }
  if(!reasons.length && notification.last_error){
    for(const raw of String(notification.last_error).split(';')){
      const match = raw.match(/^(inapp|email|sms|whatsapp|kakao|webhook):(.+)$/);
      reasons.push(match
        ? `${NTF_channelName(match[1])}: ${NTF_reasonText(match[2], notification.status)}`
        : NTF_reasonText(raw, notification.status));
    }
  }
  return reasons.map(reason => `<div data-noi18n>${esc(reason)}</div>`).join('') || '<span class="muted">—</span>';
}
