// VFP: The session token of this browser tab — taken once from the URL fragment the terminal opened, then kept out of the address bar.
// Changes when: the way the terminal hands its token to the browser changes.
// Anti-goal:
// 1. Keeping the token in the URL — it would end up in history, bookmarks and screenshots.
// 2. localStorage — the token is per terminal run and per tab, sessionStorage is enough.

const KEY = "ludik.session";

export function takeSessionToken(): string | null {
  const match = window.location.hash.match(/(?:^#|&)t=([^&]+)/);
  if (match) {
    const token = decodeURIComponent(match[1]);
    try {
      sessionStorage.setItem(KEY, token);
    } catch {
      // storage may be blocked; the token still works for this page load
    }
    history.replaceState(null, "", window.location.pathname + window.location.search);
    return token;
  }
  try {
    return sessionStorage.getItem(KEY);
  } catch {
    return null;
  }
}

export async function apiGet<T>(path: string, token: string): Promise<T> {
  const response = await fetch(path, { headers: { Authorization: `Bearer ${token}` } });
  if (!response.ok) {
    throw new Error(`${path}: ${response.status}`);
  }
  return (await response.json()) as T;
}
