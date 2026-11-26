export function clock(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined || Number.isNaN(seconds)) return '–'
  const s = Math.max(0, seconds)
  const m = Math.floor(s / 60)
  const rest = s - m * 60
  return `${m}:${rest.toFixed(1).padStart(4, '0')}`
}

export function clockShort(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return '–'
  const s = Math.round(seconds)
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`
}

export function usd(value: number | null | undefined, digits = 2): string {
  if (value === null || value === undefined) return '–'
  if (value > 0 && value < 0.01) return '<$0.01'
  return `$${value.toFixed(digits)}`
}

export function ago(epochSeconds: number): string {
  const diff = Date.now() / 1000 - epochSeconds
  if (diff < 60) return 'just now'
  if (diff < 3600) return `${Math.floor(diff / 60)} min ago`
  if (diff < 86400) return `${Math.floor(diff / 3600)} h ago`
  const days = Math.floor(diff / 86400)
  if (days < 14) return `${days} day${days === 1 ? '' : 's'} ago`
  return new Date(epochSeconds * 1000).toLocaleDateString()
}

export function tokens(n: number): string {
  return n >= 1000 ? `${(n / 1000).toFixed(n >= 10000 ? 0 : 1)}k` : String(n)
}

export function cx(...parts: (string | false | null | undefined)[]): string {
  return parts.filter(Boolean).join(' ')
}
