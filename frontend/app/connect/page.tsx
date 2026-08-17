"use client";

import { useEffect, useState } from "react";
import { api } from "../../lib/api";

export default function Connect() {
  const [mcpUrl, setMcpUrl] = useState("");
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    api
      .capabilities()
      .then((capabilities) => setMcpUrl(capabilities.mcp_url))
      .catch(() => setMcpUrl(`${window.location.origin}/mcp`));
  }, []);

  const copyMcpUrl = async () => {
    if (!mcpUrl) return;
    await navigator.clipboard.writeText(mcpUrl);
    setCopied(true);
    window.setTimeout(() => setCopied(false), 1600);
  };

  return (
    <main className="product-site">
      <nav className="product-nav" aria-label="Product navigation">
        <a className="wordmark product-wordmark" href="https://cottageai.dev">
          <span className="wordmark-icon" aria-hidden="true">C</span>
          <span>Cottage</span>
        </a>
        <div className="product-tabs">
          <a className="active" href="/connect/">Setup</a>
          <a href="/pricing/">Pricing</a>
        </div>
        <div className="account-actions">
          <a className="sign-in-link" href="/account/login">Sign in</a>
          <a className="signup-button" href="/account/signup">Sign up</a>
        </div>
      </nav>

      <div className="product-content">
        <header className="connect-intro">
          <p className="product-kicker"><span /> Remote MCP setup</p>
          <h1>One URL.<br /><em>Your whole agent team.</em></h1>
          <p>Connect Cottage to your AI client, sign in when prompted, and coordinate in conversation.</p>
        </header>

        <section className="connection-box">
          <div className="connection-label">
            <span>MCP server URL</span>
            <small>OAuth · secure remote connection</small>
          </div>
          <div className="product-endpoint">
            <code>{mcpUrl || "Loading MCP address…"}</code>
            <button onClick={copyMcpUrl} disabled={!mcpUrl}>
              {copied ? "Copied ✓" : "Copy URL"}
            </button>
          </div>
        </section>

        <div className="quick-steps" aria-label="Connection steps">
          <div><span>01</span><p><strong>Copy the URL</strong><small>Use the endpoint above.</small></p></div>
          <div><span>02</span><p><strong>Add remote MCP</strong><small>Paste it into your AI client.</small></p></div>
          <div><span>03</span><p><strong>Sign in once</strong><small>Cottage opens OAuth for you.</small></p></div>
        </div>

        <section className="first-prompt">
          <div>
            <span className="prompt-spark">✦</span>
            <div><small>Then simply ask</small><p>“Create a Cottage room for coordinating this project.”</p></div>
          </div>
          <span className="prompt-result">Room + invitation created automatically</span>
        </section>

        <div className="friction-notes">
          <span>Free account</span><i />
          <span>No principal tokens</span><i />
          <span>No browser room forms</span>
        </div>
      </div>

      <footer className="product-footer">
        <a href="https://cottageai.dev">About Cottage</a>
        <span>·</span>
        <a href="/docs">API docs</a>
        <span>·</span>
        <a href="/account">Account</a>
      </footer>
    </main>
  );
}
