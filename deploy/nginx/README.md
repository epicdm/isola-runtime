# isola-runtime nginx configs

Reference copies of the nginx server configs live here so the production
setup on 66.118.37.12 is version-controlled and reproducible.

## runtime.epic.dm.conf

Public vhost that fronts the isolaruntime-backend container at 127.0.0.1:8800.
Installed by copying into /etc/nginx/sites-available/ and symlinking to
sites-enabled/. TLS cert acquired via certbot --nginx -d runtime.epic.dm
(post-acquisition certbot rewrites this file to add the listen 443 block
and the HTTP -> HTTPS redirect; the committed version is the pre-certbot
seed plus a reminder comment).

Install flow on a fresh host (Ubuntu + nginx + certbot already installed):

    sudo cp deploy/nginx/runtime.epic.dm.conf /etc/nginx/sites-available/
    sudo ln -sf /etc/nginx/sites-available/runtime.epic.dm                 /etc/nginx/sites-enabled/runtime.epic.dm
    sudo nginx -t && sudo systemctl reload nginx
    sudo certbot --nginx -d runtime.epic.dm          --non-interactive --agree-tos --email eric@epic.dm --redirect

Certbot sets up a systemd timer for automatic renewal.

## Why proxy_read_timeout 300s

The /ws/chat/{agent_id} WebSocket + the /api/channel/whatsapp/{id}/webhook
POST handler both spawn LLM tool-calling loops that can legitimately run
up to a minute or two before returning. Nginx's default 60s proxy_read
timeout drops the connection mid-answer. 300s is generous but covers
50-round tool-calling plus media downloads.
