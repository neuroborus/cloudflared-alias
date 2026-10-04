(() => {
    "use strict";
    const config = __ALIAS_RELOAD_CONFIG__;
    if (typeof window.EventSource !== "function") return;

    let subscription = null;
    let reloading = false;

    function close() {
        if (subscription) subscription.close();
        subscription = null;
    }

    function subscribe() {
        close();
        if (reloading || document.visibilityState === "hidden") return;
        let connection;
        try {
            const events = new URL(config.events, window.location.href);
            connection = new window.EventSource(events.href);
        } catch (_) {
            return; // A blocked subscription still permits ordinary refresh.
        }
        subscription = connection;
        connection.addEventListener("revision", (event) => {
            if (subscription !== connection || reloading) return;
            let received;
            try {
                received = JSON.parse(event.data);
            } catch (_) {
                return;
            }
            if (!received || typeof received.revision !== "string" ||
                    !/^[a-f0-9]{64}$/.test(received.revision)) return;
            if (received.revision !== config.revision) {
                reloading = true;
                close();
                window.location.reload();
            }
        });
        // EventSource owns reconnection after network errors; no timer is needed.
    }

    document.addEventListener("visibilitychange", () => {
        if (document.visibilityState === "hidden") close();
        else subscribe();
    });
    window.addEventListener("pagehide", close);
    window.addEventListener("pageshow", (event) => {
        if (event.persisted) subscribe();
    });
    subscribe();
})();
