import { Link, useParams } from "react-router-dom";
import { ChevronLeft } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";

/** Day 1 stub. Real implementation lands in Week 4 Day 3. */
export default function ProjectDetail() {
  const { projectId } = useParams();
  return (
    <section className="space-y-6">
      <div>
        <Button asChild variant="ghost" size="sm">
          <Link to="/projects">
            <ChevronLeft className="size-4" />
            Back to projects
          </Link>
        </Button>
      </div>
      <Card>
        <CardHeader>
          <CardTitle>Project {projectId}</CardTitle>
        </CardHeader>
        <CardContent className="text-sm text-muted-foreground">
          Reviews list + commit attribution + branch-events banner land in Week 4 Day 3.
        </CardContent>
      </Card>
    </section>
  );
}
