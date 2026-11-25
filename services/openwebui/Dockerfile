# Open WebUI with a few small additions. Branding and licence files are untouched.
#  1. First-visit defaults: dark theme and the chat sidebar open (both stay user-changeable;
#     the sidebar still collapses and expands with its own button).
#  2. The Stratum theme: one stylesheet and three images, loaded through Open WebUI's own
#     /static/custom.css hook.
FROM ghcr.io/open-webui/open-webui:v0.13.4
RUN sed -i "s/if (!localStorage?.theme) {/if (!localStorage.stratumDefaults) { localStorage.theme = 'dark'; localStorage.sidebar = 'true'; localStorage.stratumDefaults = '1'; }\n\t\t\t\tif (!localStorage?.theme) {/" /app/build/index.html \
 && grep -q "localStorage.stratumDefaults = '1'" /app/build/index.html
COPY theme/ /app/build/static/
# Stamp the stylesheet link with a hash of its contents so browsers never show a stale copy.
RUN v=$(cat /app/build/static/custom.css /app/build/static/strata-*.svg | md5sum | cut -c1-10) \
 && sed -i "s#/static/custom.css#/static/custom.css?v=$v#g" /app/build/index.html \
 && sed -i "s#/static/strata-\([0-9]\).svg#/static/strata-\1.svg?v=$v#g" /app/build/static/custom.css \
 && grep -q "custom.css?v=$v" /app/build/index.html
