import {
  CSSProperties,
  ReactNode,
  useCallback,
  useLayoutEffect,
  useRef,
} from "react";
import { ActionIcon, Group } from "@mantine/core";
import {
  closestCenter,
  DndContext,
  DragEndEvent,
  KeyboardSensor,
  PointerSensor,
  useSensor,
  useSensors,
} from "@dnd-kit/core";
import {
  arrayMove,
  SortableContext,
  sortableKeyboardCoordinates,
  useSortable,
  verticalListSortingStrategy,
} from "@dnd-kit/sortable";
import { CSS } from "@dnd-kit/utilities";
import {
  faArrowDown,
  faArrowUp,
  faGripVertical,
} from "@fortawesome/free-solid-svg-icons";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";

export interface SortableListProps<T> {
  items: T[];
  getId: (item: T) => string;
  /** A name for an item, used in the labels of its handle and buttons. */
  getName: (item: T) => string;
  renderItem: (item: T, index: number) => ReactNode;
  /** The new order, and which item was moved to which place (0-based) in it. */
  onReorder: (ids: string[], moved: { id: string; index: number }) => void;
  disabled?: boolean;
  /** A class for each row, so a caller can draw the row as a card. */
  rowClassName?: string;
  /** Content shown under a row at the row's full width, below its handle and buttons. */
  renderDetail?: (item: T, index: number) => ReactNode;
}

interface RowProps {
  id: string;
  name: string;
  index: number;
  count: number;
  disabled: boolean;
  onMove: (from: number, to: number) => void;
  className?: string;
  detail?: ReactNode;
  children: ReactNode;
}

function SortableRow({
  id,
  name,
  index,
  count,
  disabled,
  onMove,
  className,
  detail,
  children,
}: RowProps) {
  const {
    attributes,
    listeners,
    setNodeRef,
    transform,
    transition,
    isDragging,
  } = useSortable({ id, disabled });
  const upRef = useRef<HTMLButtonElement>(null);
  const downRef = useRef<HTMLButtonElement>(null);
  // The button that was pressed, for the one draw that press causes.
  const pressed = useRef<"up" | "down" | null>(null);
  // A move to the first or last place disables the button that made it, and a disabled button drops the
  // focus. The focus goes to the row's other button, so the keyboard stays on the row that was moved.
  //
  // The flag belongs to its own press. It is read on the next draw, whatever that draw changed, and
  // dropped there. A caller that does not apply the move draws nothing, so the press drops the flag
  // itself once it is over: a later change of order, made by something else, never finds it.
  useLayoutEffect(() => {
    if (pressed.current === "up" && index === 0) {
      downRef.current?.focus();
    } else if (pressed.current === "down" && index === count - 1) {
      upRef.current?.focus();
    }
    pressed.current = null;
  });
  const press = (direction: "up" | "down") => {
    pressed.current = direction;
    onMove(index, direction === "up" ? index - 1 : index + 1);
    // A move that is applied is drawn before this runs. One that is not leaves the flag for this to drop.
    window.setTimeout(() => {
      pressed.current = null;
    }, 0);
  };
  const style: CSSProperties = {
    transform: CSS.Transform.toString(transform),
    transition,
    opacity: isDragging ? 0.85 : undefined,
    zIndex: isDragging ? 10 : undefined,
  };
  return (
    <div ref={setNodeRef} style={style} className={className} role="listitem">
      <Group gap="xs" wrap="nowrap" data-sortable-row="">
        <ActionIcon
          variant="subtle"
          color="gray"
          aria-label={`Drag ${name}`}
          disabled={disabled}
          style={{ cursor: disabled ? "default" : "grab", touchAction: "none" }}
          {...attributes}
          {...listeners}
        >
          <FontAwesomeIcon icon={faGripVertical} />
        </ActionIcon>
        <div style={{ flex: 1, minWidth: 0 }}>{children}</div>
        <ActionIcon
          variant="subtle"
          color="gray"
          ref={upRef}
          aria-label={`Move ${name} up`}
          disabled={disabled || index === 0}
          onClick={() => press("up")}
        >
          <FontAwesomeIcon icon={faArrowUp} />
        </ActionIcon>
        <ActionIcon
          variant="subtle"
          color="gray"
          ref={downRef}
          aria-label={`Move ${name} down`}
          disabled={disabled || index === count - 1}
          onClick={() => press("down")}
        >
          <FontAwesomeIcon icon={faArrowDown} />
        </ActionIcon>
      </Group>
      {detail}
    </div>
  );
}

/**
 * A vertical list the reader reorders by dragging a handle, with the keyboard
 * (the handle, Space, the arrow keys) or with the up and down buttons. It
 * knows nothing about what an item is.
 */
export function SortableList<T>({
  items,
  getId,
  getName,
  renderItem,
  onReorder,
  disabled = false,
  rowClassName,
  renderDetail,
}: SortableListProps<T>) {
  const ids = items.map(getId);
  const sensors = useSensors(
    useSensor(PointerSensor, { activationConstraint: { distance: 4 } }),
    useSensor(KeyboardSensor, {
      coordinateGetter: sortableKeyboardCoordinates,
    }),
  );
  const move = useCallback(
    (from: number, to: number) => {
      if (to < 0 || to >= ids.length || from === to) {
        return;
      }
      onReorder(arrayMove(ids, from, to), { id: ids[from], index: to });
    },
    [ids, onReorder],
  );
  const handleDragEnd = (event: DragEndEvent) => {
    const { active, over } = event;
    if (!over || active.id === over.id) {
      return;
    }
    move(ids.indexOf(String(active.id)), ids.indexOf(String(over.id)));
  };
  return (
    <DndContext
      sensors={sensors}
      collisionDetection={closestCenter}
      onDragEnd={handleDragEnd}
    >
      <SortableContext items={ids} strategy={verticalListSortingStrategy}>
        <div role="list" style={{ display: "grid", gap: 8 }}>
          {items.map((item, index) => (
            <SortableRow
              key={getId(item)}
              id={getId(item)}
              name={getName(item)}
              index={index}
              count={items.length}
              disabled={disabled}
              onMove={move}
              className={rowClassName}
              detail={renderDetail?.(item, index)}
            >
              {renderItem(item, index)}
            </SortableRow>
          ))}
        </div>
      </SortableContext>
    </DndContext>
  );
}

export default SortableList;
