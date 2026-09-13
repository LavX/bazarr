import { Link } from "react-router";
import UniversalSearch from "@/components/UniversalSearch";
import { useDiscover } from "@/contexts/Discover";
import Discover from ".";

// Page tests include the real shared search; App shell tests verify its placement.
export default function DiscoverTestPage() {
  const { state } = useDiscover();
  return (
    <>
      <UniversalSearch />
      <nav aria-label="Test app navigation">
        {state.draft.mode !== "release" && (
          <Link to="/subtitle-hub">Subtitle Hub</Link>
        )}
        <Link to="/history/series">History</Link>
      </nav>
      <Discover />
    </>
  );
}
