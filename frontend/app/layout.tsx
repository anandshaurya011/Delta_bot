import { ClerkProvider } from "@clerk/nextjs";
import type { ReactNode } from "react";
import "./globals.css";
import { Providers } from "./providers";

export const metadata = { title: "Delta Trader" };
export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <ClerkProvider>
      <html lang="en"><body><Providers>{children}</Providers></body></html>
    </ClerkProvider>
  );
}
