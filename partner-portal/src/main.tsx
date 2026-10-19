import React from "react";
import ReactDOM from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { BrowserRouter } from "react-router-dom";
import App from "./App";
import { ApiError } from "./api/client";
import { initAuth } from "./auth";
import "./styles/theme.css";
import "./styles/app.css";

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      // 401/403/404 will not change on a retry.
      retry: (count, err) =>
        !(err instanceof ApiError && [401, 403, 404].includes(err.status)) && count < 1,
      refetchOnWindowFocus: false,
    },
  },
});

initAuth()
  .then(() => {
    ReactDOM.createRoot(document.getElementById("root")!).render(
      <React.StrictMode>
        <QueryClientProvider client={queryClient}>
          <BrowserRouter>
            <App />
          </BrowserRouter>
        </QueryClientProvider>
      </React.StrictMode>
    );
  })
  .catch((err) => {
    const div = document.createElement("div");
    div.style.cssText = "padding:40px;font-family:sans-serif";
    div.textContent = `Failed to initialise: ${String(err)}`;
    document.getElementById("root")!.replaceChildren(div);
  });
