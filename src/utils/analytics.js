// Basic server-side pageview tracking (same-origin, no third-party scripts)
const getApiBaseUrl = () => {
  if (process.env.NODE_ENV === 'production') {
    return process.env.REACT_APP_API_BASE_URL || window.location.origin;
  }
  return process.env.REACT_APP_API_BASE_URL || 'http://localhost:8000';
};

export function trackPageview(path, title) {
  const apiBaseUrl = getApiBaseUrl();
  fetch(`${apiBaseUrl}/api/track-view`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ path, title }),
  }).catch(() => {}); // fire-and-forget; tracking must never surface an error to the user
}
