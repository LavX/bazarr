import { customRender, screen } from "@/tests";
import HistoryProvider from "./HistoryProvider";

it("marks provider AI translations and keeps host translation action distinct", () => {
  const view = customRender(
    <HistoryProvider provider="subdl" aiTranslated action={1} />,
  );
  expect(screen.getByText("subdl")).toBeInTheDocument();
  expect(screen.getByLabelText("AI-translated")).toHaveTextContent("AI");

  view.rerender(
    <HistoryProvider provider="subdl" aiTranslated={false} action={1} />,
  );
  expect(screen.queryByText("AI")).not.toBeInTheDocument();
  view.rerender(
    <HistoryProvider provider="translator" aiTranslated action={6} />,
  );
  expect(screen.queryByText("AI")).not.toBeInTheDocument();
});
