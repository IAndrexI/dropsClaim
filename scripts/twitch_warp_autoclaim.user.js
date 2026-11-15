// ==UserScript==
// @name         Twitch Drops Auto-WARP Claimer
// @namespace    https://github.com/IAndrexI/dropsClaim
// @version      1.0
// @description  Automatically turns Cloudflare WARP ON when clicking 'Claim Now' on Twitch, then turns it back OFF.
// @author       Auto Loot Claimer
// @match        https://www.twitch.tv/drops/*
// @grant        GM_xmlhttpRequest
// @connect      127.0.0.1
// ==/UserScript==

(function() {
    'use strict';

    const BRIDGE_URL = "http://127.0.0.1:8765/claim?duration=8";

    function triggerWarp(callback) {
        console.log("[Twitch-WARP] Triggering WARP for claim window...");
        fetch(BRIDGE_URL, { method: "POST", mode: "cors" })
            .then(res => res.json())
            .then(data => {
                console.log("[Twitch-WARP] Response:", data);
            })
            .catch(err => {
                console.log("[Twitch-WARP] Local bridge note (make sure warp_claim_helper is running):", err);
            });

        // Give WARP 1.5s to establish tunnel before triggering DOM click if callback provided
        if (typeof callback === "function") {
            setTimeout(callback, 1500);
        }
    }

    // Intercept clicks on Twitch 'Claim Now' buttons
    document.addEventListener("click", function(e) {
        const target = e.target.closest("button") || e.target;
        const btnText = (target.textContent || "").trim().toLowerCase();
        if (btnText === "claim now" || btnText === "claim" || target.getAttribute("data-test-selector") === "DropsCampaignInProgressReward-claim-button") {
            if (!target.dataset.warpHandled) {
                target.dataset.warpHandled = "true";
                triggerWarp();
                console.log("[Twitch-WARP] 'Claim Now' clicked! Cloudflare WARP activated for 8 seconds.");
            }
        }
    }, true);

    // Optional: Add a top banner button "⚡ Claim All with WARP" on the inventory page
    function injectWarpButton() {
        if (document.getElementById("warp-autoclaim-btn")) return;
        const header = document.querySelector("h1, h2, .drops-root") || document.body;
        if (!header) return;

        const btn = document.createElement("button");
        btn.id = "warp-autoclaim-btn";
        btn.textContent = "⚡ Auto-Claim with WARP";
        btn.style.cssText = "position:fixed;bottom:20px;right:20px;z-index:999999;padding:10px 18px;background:#9146FF;color:#fff;border:none;border-radius:20px;font-weight:bold;cursor:pointer;box-shadow:0 4px 12px rgba(0,0,0,0.4);font-family:inherit;";
        
        btn.onclick = function() {
            btn.textContent = "Connecting WARP...";
            btn.style.background = "#F48024";
            triggerWarp(function() {
                // Click all visible Claim Now buttons
                const claimButtons = Array.from(document.querySelectorAll("button")).filter(b => (b.textContent || "").toLowerCase().includes("claim now"));
                claimButtons.forEach(b => b.click());
                btn.textContent = `Claimed ${claimButtons.length} Drop(s)!`;
                btn.style.background = "#2ea44f";
                setTimeout(() => {
                    btn.textContent = "⚡ Auto-Claim with WARP";
                    btn.style.background = "#9146FF";
                }, 8000);
            });
        };
        document.body.appendChild(btn);
    }

    window.addEventListener("load", injectWarpButton);
    setInterval(injectWarpButton, 2000);
})();
