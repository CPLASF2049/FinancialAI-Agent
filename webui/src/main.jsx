import React from "react";
import ReactDOM from "react-dom/client";
import ClaudeCodeUI from "./ClaudeCodeUI.jsx";
import "./index.css";

// apiBase 留空：与后端同源部署，走相对路径
ReactDOM.createRoot(document.getElementById("root")).render(
  <React.StrictMode>
    <ClaudeCodeUI />
  </React.StrictMode>
);
