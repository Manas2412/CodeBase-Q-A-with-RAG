import { Link } from "react-router-dom";
import { Button } from "@/components/ui/button";

export default function NotFound() {
  return (
    <section className="flex flex-col items-center justify-center gap-4 py-24 text-center">
      <h1 className="text-3xl font-semibold">Not found</h1>
      <p className="text-muted-foreground">
        The page you're looking for doesn't exist on this dashboard.
      </p>
      <Button asChild>
        <Link to="/projects">Back to projects</Link>
      </Button>
    </section>
  );
}
