const express = require("express");
const https = require("https");

const app = express();
const PORT = 3000;

const CLIENT_ID = process.env.BOT_SLACK_CLIENT_ID;
const CLIENT_SECRET = process.env.BOT_SLACK_CLIENT_SECRET;
const REDIRECT_URI = "https://slack.awsm.jp/callback";
const SCOPES = "chat:write,channels:read";

app.get("/", (_req, res) => {
  const url =
    `https://slack.com/oauth/v2/authorize` +
    `?client_id=${CLIENT_ID}` +
    `&scope=${SCOPES}` +
    `&redirect_uri=${encodeURIComponent(REDIRECT_URI)}`;

  res.send(`
    <!DOCTYPE html>
    <html><head><meta charset="utf-8"><title>Slack Auth</title>
    <style>
      body { font-family: sans-serif; display: flex; justify-content: center; align-items: center; height: 100vh; margin: 0; background: #f4f4f4; }
      a { display: inline-block; padding: 16px 32px; background: #4A154B; color: #fff; text-decoration: none; border-radius: 8px; font-size: 18px; }
      a:hover { background: #611f69; }
    </style>
    </head><body>
      <a href="${url}">Add to Slack</a>
    </body></html>
  `);
});

app.get("/callback", (req, res) => {
  const { code } = req.query;
  if (!code) return res.status(400).send("Missing code parameter");

  const params = new URLSearchParams({
    client_id: CLIENT_ID,
    client_secret: CLIENT_SECRET,
    code,
    redirect_uri: REDIRECT_URI,
  });

  const options = {
    hostname: "slack.com",
    path: "/api/oauth.v2.access",
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
  };

  const apiReq = https.request(options, (apiRes) => {
    let body = "";
    apiRes.on("data", (chunk) => (body += chunk));
    apiRes.on("end", () => {
      const data = JSON.parse(body);
      if (!data.ok) return res.status(400).send(`Error: ${data.error}`);

      const token = data.access_token;
      const team = data.team?.name || "";

      res.send(`
        <!DOCTYPE html>
        <html><head><meta charset="utf-8"><title>Slack Auth - Token</title>
        <style>
          body { font-family: sans-serif; display: flex; justify-content: center; align-items: center; height: 100vh; margin: 0; background: #f4f4f4; }
          .card { background: #fff; padding: 32px; border-radius: 12px; box-shadow: 0 2px 8px rgba(0,0,0,0.1); max-width: 600px; word-break: break-all; }
          h2 { color: #4A154B; }
          code { background: #f0f0f0; padding: 8px 12px; border-radius: 4px; display: block; margin-top: 8px; font-size: 14px; }
        </style>
        </head><body>
          <div class="card">
            <h2>Authenticated! (${team})</h2>
            <p><strong>Bot Token:</strong></p>
            <code>${token}</code>
          </div>
        </body></html>
      `);
    });
  });

  apiReq.on("error", (err) => res.status(500).send(`Request failed: ${err.message}`));
  apiReq.write(params.toString());
  apiReq.end();
});

app.listen(PORT, () => {
  console.log(`Slack Auth server running on http://localhost:${PORT}`);
});
