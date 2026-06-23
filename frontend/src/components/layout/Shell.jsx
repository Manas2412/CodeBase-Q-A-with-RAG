import { Link, NavLink, Outlet } from "react-router-dom";
import { Terminal, FolderGit2, Plus } from "lucide-react";

import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

/**
 * App shell that wraps every route — top nav + max-width main column.
 *
 * NavLink renders `aria-current="page"` on the active route, which the
 * `[&[aria-current=page]]:` Tailwind selector picks up. Keeps active styles
 * declarative instead of threading a useLocation hook through.
 */
function TopNav() {
  return (
    <header className="sticky top-0 z-30 w-full border-b border-border/40 bg-background/80 backdrop-blur">
      <div className="container flex h-14 items-center justify-between">
        <Link to="/" className="flex items-center gap-2 font-semibold tracking-tight">
          <Terminal className="size-5 text-primary" />
          <span>Code Review Agent</span>
        </Link>

        <nav className="flex items-center gap-1">
          <NavLink
            to="/projects"
            className={({ isActive }) =>
              cn(
                "inline-flex items-center gap-2 rounded-md px-3 py-1.5 text-sm font-medium text-muted-foreground hover:bg-accent hover:text-accent-foreground",
                isActive && "bg-accent text-accent-foreground"
              )
            }
          >
            <FolderGit2 className="size-4" />
            Projects
          </NavLink>

          <Button asChild size="sm" className="ml-2">
            <Link to="/projects/new">
              <Plus className="size-4" />
              New project
            </Link>
          </Button>
        </nav>
      </div>
    </header>
  );
}

export default function Shell() {
  return (
    <div className="min-h-screen bg-background text-foreground">
      <TopNav />
      <main className="container py-8">
        <Outlet />
      </main>
    </div>
  );
}
