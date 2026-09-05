import { RelayShell } from "@/app/relay-shell";

/**
 * Relay's root route. A Server Component whose only job is to mount the
 * client shell — all interactive state lives in `RelayShell`.
 */
export default function App() {
  return <RelayShell />;
}
