# Open WebUI with a few small additions. Branding and licence files are untouched.
#  1. First-visit defaults: dark theme and the chat sidebar open (both stay user-changeable;
#     the sidebar still collapses and expands with its own button).
#  2. The Stratum theme: one stylesheet and three images, loaded through Open WebUI's own
#     /static/custom.css hook.
FROM ghcr.io/open-webui/open-webui:v0.13.4
RUN sed -i "s/if (!localStorage?.theme) {/if (!localStorage.stratumDefaults) { localStorage.theme = 'dark'; localStorage.sidebar = 'true'; localStorage.stratumDefaults = '1'; }\n\t\t\t\tif (!localStorage?.theme) {/" /app/build/index.html \
 && grep -q "localStorage.stratumDefaults = '1'" /app/build/index.html
COPY theme/ /app/build/static/
