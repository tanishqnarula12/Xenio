import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { App, SignIn } from "./App";
import { HttpApi, readToken, type XenioApi } from "./api/client";
import { AppController } from "./state/controller";
import { AppProvider } from "./state/context";
import { Store } from "./state/store";
import "./styles/tokens.css";
import "./styles/base.css";
import "./styles/app.css";

async function choose(): Promise<XenioApi | null> {
  // Demo data only when the page was explicitly built or served for it; never as a fallback for a real XENIO.
  if (import.meta.env.VITE_XENIO_DEMO === "1") {
    const { DemoApi } = await import("./api/demo");
    return new DemoApi();
  }
  const token = readToken();
  return token ? new HttpApi(token) : null;
}

const root = createRoot(document.getElementById("root")!);
void choose().then((api) => {
  if (!api) {
    root.render(<StrictMode><SignIn /></StrictMode>);
    return;
  }
  const store = new Store();
  const app = new AppController(api, store);
  void app.init();
  window.addEventListener("pagehide", () => app.dispose());
  root.render(
    <StrictMode>
      <AppProvider store={store} app={app}><App /></AppProvider>
    </StrictMode>,
  );
});
