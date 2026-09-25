import { RowData } from "@tanstack/react-table";

declare module "@tanstack/react-table" {
  // Per-column extra class applied to the header and body cells of a column.
  // Used to control column width/overflow from the column definition (e.g.
  // keeping compact columns content-sized while one column absorbs the rest).
  // eslint-disable-next-line @typescript-eslint/no-unused-vars
  interface ColumnMeta<TData extends RowData, TValue> {
    className?: string;
  }
}
