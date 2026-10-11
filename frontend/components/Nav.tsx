"use client";
import Link from "next/link";
import { usePathname } from "next/navigation";

const LINKS = [
  { href: "/", label: "Dashboard" },
  { href: "/trade", label: "Trade" },
  { href: "/orders", label: "Orders" },
  { href: "/analysis", label: "Analysis" },
  { href: "/history", label: "History" },
  { href: "/settings", label: "Settings" },
];

export function Nav() {
  const pathname = usePathname();
  return (
    <nav>
      {LINKS.map((l) => {
        const active = l.href === "/" ? pathname === "/" : pathname?.startsWith(l.href);
        return (
          <Link key={l.href} href={l.href} className={active ? "on" : undefined}>
            {l.label}
          </Link>
        );
      })}
    </nav>
  );
}
