"use client";
import Link from "next/link";
import { usePathname } from "next/navigation";

const LINKS = [
  { href: "/", label: "Dashboard" },
  { href: "/trade", label: "Trade" },
  { href: "/orders", label: "Orders" },
  { href: "/analysis", label: "Analysis" },
  { href: "/ai-trading", label: "AI Trading" },
  { href: "/ai-trading/chat", label: "Agent" },
  { href: "/history", label: "History" },
  { href: "/settings", label: "Settings" },
];

export function Nav() {
  const pathname = usePathname();
  return (
    <nav>
      {LINKS.map((l) => {
        // "/ai-trading" and "/ai-trading/chat" are separate nav entries that both
        // start with "/ai-trading" -- treat "/ai-trading" as active only for paths
        // under it that AREN'T the more specific "/ai-trading/chat" entry.
        const active =
          l.href === "/"
            ? pathname === "/"
            : l.href === "/ai-trading"
              ? pathname?.startsWith(l.href) && !pathname?.startsWith("/ai-trading/chat")
              : pathname?.startsWith(l.href);
        return (
          <Link key={l.href} href={l.href} className={active ? "on" : undefined}>
            {l.label}
          </Link>
        );
      })}
    </nav>
  );
}
