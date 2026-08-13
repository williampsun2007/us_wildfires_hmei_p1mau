import React, { useState } from 'react';
import {
  Box,
  Container,
  Paper,
  Typography,
  TextField,
  Button,
  Stack,
  Alert,
  Table,
  TableHead,
  TableBody,
  TableRow,
  TableCell,
} from '@mui/material';
import { getApiBaseUrl } from '../utils/analytics';

// Unlisted admin page (not in any nav) that lists individual pageview rows
// (path, title, country, timestamp) from GET /api/pageviews. Gated
// server-side by the ADMIN_API_KEY shared secret - this page is just a
// friendly wrapper around that endpoint, not the actual access control.
const KEY_STORAGE_KEY = 'wildfire_admin_key';

function PageViewLog() {
  const [apiKey, setApiKey] = useState(() => sessionStorage.getItem(KEY_STORAGE_KEY) || '');
  const [keyInput, setKeyInput] = useState('');
  const [records, setRecords] = useState(null);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);

  const fetchPageviews = (activeKey) => {
    setLoading(true);
    setError('');
    fetch(`${getApiBaseUrl()}/api/pageviews?limit=500`, {
      headers: { 'X-Admin-Key': activeKey },
    })
      .then((res) => {
        if (res.status === 401) {
          sessionStorage.removeItem(KEY_STORAGE_KEY);
          setApiKey('');
          throw new Error('Invalid or missing admin key.');
        }
        if (!res.ok) throw new Error(`Request failed (${res.status})`);
        return res.json();
      })
      .then((data) => setRecords(data))
      .catch((err) => setError(err.message))
      .finally(() => setLoading(false));
  };

  React.useEffect(() => {
    if (apiKey) fetchPageviews(apiKey);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [apiKey]);

  const handleSubmitKey = (e) => {
    e.preventDefault();
    if (!keyInput.trim()) return;
    sessionStorage.setItem(KEY_STORAGE_KEY, keyInput.trim());
    setApiKey(keyInput.trim());
  };

  return (
    <Container maxWidth="lg" sx={{ py: 4, px: 4 }}>
      <Typography variant="h4" component="h1" gutterBottom className="page-title" sx={{ ml: 4 }}>
        Pageview Log
      </Typography>

      {!apiKey && (
        <Paper elevation={2} sx={{ p: 4, mb: 4, ml: 4, mr: 4 }}>
          <Typography variant="body1" gutterBottom>
            Enter the admin API key to view pageview history.
          </Typography>
          <Box component="form" onSubmit={handleSubmitKey}>
            <Stack direction="row" spacing={2}>
              <TextField
                type="password"
                label="Admin API key"
                size="small"
                fullWidth
                value={keyInput}
                onChange={(e) => setKeyInput(e.target.value)}
              />
              <Button type="submit" variant="contained">View</Button>
            </Stack>
          </Box>
          {error && <Alert severity="error" sx={{ mt: 2 }}>{error}</Alert>}
        </Paper>
      )}

      {apiKey && (
        <Paper elevation={2} sx={{ p: 4, mb: 4, ml: 4, mr: 4 }}>
          {loading && <Typography variant="body2">Loading…</Typography>}
          {error && <Alert severity="error" sx={{ mb: 2 }}>{error}</Alert>}
          {records && (
            <>
              <Typography variant="body2" sx={{ mb: 2 }}>
                Showing {records.length} most recent pageview{records.length === 1 ? '' : 's'}.
              </Typography>
              <Box sx={{ overflowX: 'auto' }}>
                <Table size="small">
                  <TableHead>
                    <TableRow>
                      <TableCell>Timestamp</TableCell>
                      <TableCell>Page</TableCell>
                      <TableCell>Country</TableCell>
                    </TableRow>
                  </TableHead>
                  <TableBody>
                    {records.map((row, i) => (
                      <TableRow key={i}>
                        <TableCell sx={{ fontFamily: 'monospace', fontSize: '0.85rem' }}>{row.created_at}</TableCell>
                        <TableCell>{row.title || row.path}</TableCell>
                        <TableCell>{row.country || 'Unknown'}</TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              </Box>
            </>
          )}
        </Paper>
      )}
    </Container>
  );
}

export default PageViewLog;
