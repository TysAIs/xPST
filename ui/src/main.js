import { mount } from "svelte";
import "./app.css";
import App from "./App.svelte";
import { captureTokenFromFragment } from "./lib/auth-token.js";

// `xpst ui` opens this page with the dashboard API token in the URL fragment
// (`#xpst_token=…`). Consume it before the router mounts: the token is stored
// for the tab session and removed from the address bar, and the hash is
// rewritten to the landing route.
captureTokenFromFragment();

const app = mount(App, { target: document.getElementById("app") });

export default app;
