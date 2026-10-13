'use client';

import { LoaderCircle } from 'lucide-react';

export function classNames(...parts: Array<string | false | null | undefined>) {
  return parts.filter(Boolean).join(' ');
}

export function StatusDot({ online, pulse = false }: { online: boolean; pulse?: boolean }) {
  return <span className={classNames('status-dot', online ? 'online' : 'offline', pulse && 'pulse')} />;
}

export function SectionTitle({ eyebrow, title }: { eyebrow: string; title: string }) {
  return (
    <div className="section-title">
      <span>{eyebrow}</span>
      <div>
        <h2>{title}</h2>
      </div>
    </div>
  );
}

export function ActionButton({
  children,
  onClick,
  disabled,
  danger,
  busy,
  spinIcon = false,
  kind = 'primary',
}: {
  children: React.ReactNode;
  onClick: () => void;
  disabled?: boolean;
  danger?: boolean;
  busy?: boolean;
  spinIcon?: boolean;
  kind?: 'primary' | 'secondary' | 'ghost';
}) {
  return (
    <button
      type="button"
      className={classNames('action-button', `button-${kind}`, danger && 'button-danger', busy && spinIcon && 'button-icon-busy')}
      onClick={onClick}
      disabled={disabled || busy}
      aria-busy={busy || undefined}
    >
      {busy && !spinIcon ? <LoaderCircle size={17} className="spin" /> : null}
      {children}
    </button>
  );
}
