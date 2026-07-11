import { Link, NavLink, Outlet, useNavigate } from "react-router-dom";
import { Terminal, FolderGit2, Plus, LogOut } from "lucide-react";

import { Button } from "@/components/ui/button";
import { ThemeToggle } from "@/components/layout/ThemeToggle";
import { useAuth } from "@/lib/auth";
import { cn } from "@/lib/utils";

/**
 * Top-of-page floating glass nav pill — Revone-style.
 *
 * Visual layers, in order:
 *   1. Body-level radial halo (defined in index.css)
 *   2. Pill container: rounded-full, semi-transparent card colour, backdrop-blur
 *   3. Inside: brand, route links, theme toggle, gradient indigo CTA
 *
 * The pill is centred and capped at ~max-w-3xl so it stays a "ribbon" rather
 * than stretching the full viewport — that's what gives the floating feel.
 * `sticky top-4` keeps it pinned just below the viewport top with a 16px gap.
 */
function TopNav() {
  const auth = useAuth();
  const navigate = useNavigate();

  const handleLogout = () => {
    auth.signOut.mutate(undefined, {
      onSettled: () => navigate("/login", { replace: true }),
    });
  };

  return (
    <header className="sticky top-4 z-30 px-4">
      <div className="mx-auto max-w-3xl">
        <div
          className={cn(
            "flex h-12 items-center justify-between gap-2 rounded-full px-2 pl-4",
            "border border-border/60 bg-card/60 shadow-lg shadow-black/10",
            "backdrop-blur-xl backdrop-saturate-150"
          )}
        >
          <Link
            to="/"
            className="flex items-center gap-2 font-semibold tracking-tight"
          >
            <Terminal className="size-4 text-primary" />
            <span className="text-sm">Code Review Agent</span>
          </Link>

          <nav className="flex items-center gap-1">
            <NavLink
              to="/projects"
              className={({ isActive }) =>
                cn(
                  "inline-flex items-center gap-1.5 rounded-full px-3 py-1.5 text-xs font-medium text-muted-foreground transition-colors hover:bg-accent hover:text-accent-foreground",
                  isActive && "bg-accent text-accent-foreground"
                )
              }
            >
              <FolderGit2 className="size-3.5" />
              Projects
            </NavLink>

            <ThemeToggle />

            <Button
              variant="ghost"
              size="icon"
              onClick={handleLogout}
              aria-label="Sign out"
              title="Sign out"
            >
              <LogOut className="size-4" />
            </Button>

            <Button
              asChild
              size="sm"
              className={cn(
                "ml-1 rounded-full px-4 shadow-md shadow-primary/30",
                // Subtle gradient — same colour, slight lift on hover
                "bg-gradient-to-b from-primary to-primary/90 hover:to-primary/80"
              )}
            >
              <Link to="/projects/new">
                <Plus className="size-3.5" />
                New project
              </Link>
            </Button>
          </nav>
        </div>
      </div>
    </header>
  );
}

export default function Shell() {
  return (
    <div className="relative min-h-screen bg-background text-foreground">
      <TopNav />
      <main className="container py-10">
        <Outlet />
      </main>
    </div>
  );
}
