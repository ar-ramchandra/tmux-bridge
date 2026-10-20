# Roadmap

Priorities for the next releases; these are not delivery commitments.

1. Repeatable fresh-node installation on both Linux and macOS, including certificate replacement and uninstall/recovery guides.
2. Physical device checks for Safari/iOS, Android and desktop trackpads: selection, clipboard, keyboard input and scrolling.
3. Automated certificate-expiry warnings and an explicit renewal workflow.
4. Vendor browser assets for offline operation and reduce CDN dependencies.
5. A guided, verifiable setup for a new optional Cloudflare deployment.
6. Separate the embedded frontend from gateway routing without changing terminal behavior.

Per-user authorization and tenant isolation require a different security design. They are not provided by the current alpha; use it only among equally trusted operators.
