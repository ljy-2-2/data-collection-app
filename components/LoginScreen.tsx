'use client';
import { LoaderCircle } from 'lucide-react';
import { useEffect, useLayoutEffect, useRef, useState } from 'react';
import { requestJson } from '../lib/http';
import {
  hasPendingLogout,
  logoutPendingKey,
  type PlatformUser,
  type PlatformAuthStatus,
  type LoginPreferences,
} from '../lib/platformAuth';

export function LoginScreen({
  baseUrl,
  onLogin,
  forgetSaved,
  logoutBusy,
  logoutMessage,
}: {
  baseUrl: string;
  onLogin: (user: PlatformUser, warning?: string) => void;
  forgetSaved: boolean;
  logoutBusy: boolean;
  logoutMessage: string;
}) {
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState('');
  const [rememberPassword, setRememberPassword] = useState(false);
  const [autoLogin, setAutoLogin] = useState(false);
  const [useSavedPassword, setUseSavedPassword] = useState(false);
  const [savedPasswordLength, setSavedPasswordLength] = useState(0);
  const [preferencesLoading, setPreferencesLoading] = useState(!forgetSaved);
  const onLoginRef = useRef(onLogin);
  useLayoutEffect(() => {
    onLoginRef.current = onLogin;
  }, [onLogin]);

  useEffect(() => {
    if (forgetSaved) return;
    let active = true;
    const controller = new AbortController();
    // First desktop use may require creating/unlocking the system keyring.
    const timeout = window.setTimeout(() => controller.abort(), 125000);
    async function restore() {
      try {
        // If logout could not reach the service last time, finish clearing it
        // before reading preferences. This marker contains no credentials.
        if (hasPendingLogout(baseUrl)) {
          const result = await requestJson<{ warning?: string }>(
            baseUrl,
            '/platform/auth/logout',
            { method: 'POST', signal: controller.signal },
          );
          window.localStorage.removeItem(logoutPendingKey(baseUrl));
          if (active && result.warning) setMessage(result.warning);
          return;
        }
        const saved = await requestJson<LoginPreferences>(
          baseUrl,
          '/platform/auth/preferences',
          { signal: controller.signal },
        );
        if (!active) return;
        setUsername(saved.username);
        setRememberPassword(saved.remember_password);
        setAutoLogin(saved.auto_login);
        setUseSavedPassword(saved.has_saved_password);
        setSavedPasswordLength(saved.password_length || 0);
        if (saved.auto_login && saved.has_saved_password) {
          setBusy(true);
          const result = await requestJson<PlatformAuthStatus>(
            baseUrl,
            '/platform/auth/login',
            {
              method: 'POST',
              signal: controller.signal,
              body: JSON.stringify({
                username: saved.username,
                password: '',
                use_saved_password: true,
                remember_password: true,
                auto_login: true,
              }),
            },
          );
          if (!result.authenticated || !result.user)
            throw new Error('登录状态无效');
          if (active) onLoginRef.current(result.user, result.warning);
        }
      } catch (reason) {
        if (active) {
          setAutoLogin(false);
          setUseSavedPassword(false);
          setMessage(
            reason instanceof Error && reason.name === 'AbortError'
              ? '自动登录超时，请手动登录。'
              : reason instanceof Error
                ? reason.message
                : '无法恢复登录信息，请手动登录。',
          );
        }
      } finally {
        window.clearTimeout(timeout);
        if (active) {
          setBusy(false);
          setPreferencesLoading(false);
        }
      }
    }
    void restore();
    return () => {
      active = false;
      controller.abort();
      window.clearTimeout(timeout);
    };
  }, [baseUrl, forgetSaved]);

  async function submit(event: React.SyntheticEvent<HTMLFormElement>) {
    event.preventDefault();
    if (busy || preferencesLoading || logoutBusy) return;
    setBusy(true);
    setMessage('');
    try {
      if (hasPendingLogout(baseUrl)) {
        await requestJson(baseUrl, '/platform/auth/logout', { method: 'POST' });
        window.localStorage.removeItem(logoutPendingKey(baseUrl));
      }
      const result = await requestJson<PlatformAuthStatus>(
        baseUrl,
        '/platform/auth/login',
        {
          method: 'POST',
          body: JSON.stringify({
            username,
            password: useSavedPassword ? '' : password,
            remember_password: rememberPassword,
            auto_login: autoLogin,
            use_saved_password: useSavedPassword,
          }),
        },
      );
      if (!result.authenticated || !result.user)
        throw new Error('登录状态无效');
      setPassword('');
      onLogin(result.user, result.warning);
    } catch (reason) {
      setMessage(
        reason instanceof Error ? reason.message : '登录失败，请稍后再试',
      );
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="app-login-screen">
      <form className="app-login-card" onSubmit={submit}>
        <div className="app-login-brand">
          <img src="/K.png" alt="" />
          <div>
            <p>ROBOT DATA COLLECTION</p>
            <h1>登录</h1>
          </div>
        </div>
        <label>
          <span>用户名</span>
          <input
            type="text"
            autoComplete="username"
            value={username}
            onChange={(event) => {
              setUsername(event.target.value);
              setUseSavedPassword(false);
            }}
            disabled={busy || preferencesLoading || logoutBusy}
            required
          />
        </label>
        <label>
          <span>密码</span>
          <input
            type="password"
            autoComplete="current-password"
            value={
              useSavedPassword ? '*'.repeat(savedPasswordLength) : password
            }
            onFocus={(event) => {
              if (useSavedPassword) event.currentTarget.select();
            }}
            onBeforeInput={(event) => {
              if (useSavedPassword) event.currentTarget.select();
            }}
            onKeyDown={(event) => {
              if (
                useSavedPassword &&
                (event.key === 'Backspace' || event.key === 'Delete')
              ) {
                event.preventDefault();
                setPassword('');
                setUseSavedPassword(false);
              }
            }}
            onPaste={(event) => {
              if (useSavedPassword) event.currentTarget.select();
            }}
            onSelect={(event) => {
              // Saved markers are replaced as a whole when typing or pasting.
              const input = event.currentTarget;
              if (
                useSavedPassword &&
                (input.selectionStart !== 0 ||
                  input.selectionEnd !== input.value.length)
              )
                input.select();
            }}
            onChange={(event) => {
              setPassword(event.target.value);
              setUseSavedPassword(false);
            }}
            disabled={busy || preferencesLoading || logoutBusy}
            required={!useSavedPassword}
          />
        </label>
        <div className="app-login-options">
          <label>
            <input
              type="checkbox"
              checked={rememberPassword}
              disabled={busy || preferencesLoading || logoutBusy}
              onChange={(event) => {
                setRememberPassword(event.target.checked);
                if (!event.target.checked) setAutoLogin(false);
              }}
            />
            <span>记住账号密码</span>
          </label>
          <label>
            <input
              type="checkbox"
              checked={autoLogin}
              disabled={busy || preferencesLoading || logoutBusy}
              onChange={(event) => {
                setAutoLogin(event.target.checked);
                if (event.target.checked) setRememberPassword(true);
              }}
            />
            <span>自动登录</span>
          </label>
        </div>
        {message || logoutMessage ? (
          <div className="app-login-error" role="alert">
            {message || logoutMessage}
          </div>
        ) : null}
        <button
          type="submit"
          className="button-primary"
          disabled={busy || preferencesLoading || logoutBusy}
        >
          {busy ? (
            <>
              <LoaderCircle size={16} className="spin" />
              正在登录
            </>
          ) : logoutBusy ? (
            '正在清理登录信息'
          ) : preferencesLoading ? (
            '正在读取登录设置'
          ) : (
            '登录'
          )}
        </button>
      </form>
    </main>
  );
}
