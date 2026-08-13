import React from 'react';
import { createRoot } from 'react-dom/client';
import { ThemeProvider, createTheme } from '@mui/material/styles';
import CssBaseline from '@mui/material/CssBaseline';
import App from './App';
import PageViewLog from './components/PageViewLog';

// Create a theme instance
const theme = createTheme({
  palette: {
    primary: {
      main: '#e9ae40',
      light: '#f4d8a8',
      dark: '#be8a29',
    },
    secondary: {
      main: '#dc004e',
      light: '#ff6b9d',
      dark: '#a7003a',
    },
    background: {
      default: '#f5f5f5',
    },
  },
  typography: {
    fontFamily: [
      '-apple-system',
      'BlinkMacSystemFont',
      '"Segoe UI"',
      'Roboto',
      '"Helvetica Neue"',
      'Arial',
      'sans-serif',
      '"Apple Color Emoji"',
      '"Segoe UI Emoji"',
      '"Segoe UI Symbol"',
    ].join(','),
  },
});

// Get the root element
const container = document.getElementById('root');
const root = createRoot(container);

// Unlisted admin route - not linked from any nav, reachable directly by URL.
// nginx already falls back unknown paths to index.html, so no router
// dependency is needed for this one hidden page.
const isPageViewAdmin = window.location.pathname.endsWith('/admin/pageviews');

// Render the app
root.render(
  <React.StrictMode>
    <ThemeProvider theme={theme}>
      <CssBaseline />
      {isPageViewAdmin ? <PageViewLog /> : <App />}
    </ThemeProvider>
  </React.StrictMode>
);
