import { CSSProperties, ReactNode, useCallback } from "react";
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
  onReorder: (ids: string[]) => void;
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
          aria-label={`Move ${name} up`}
          disabled={disabled || index === 0}
          onClick={() => onMove(index, index - 1)}
        >
          <FontAwesomeIcon icon={faArrowUp} />
        </ActionIcon>
        <ActionIcon
          variant="subtle"
          color="gray"
          aria-label={`Move ${name} down`}
          disabled={disabled || index === count - 1}
          onClick={() => onMove(index, index + 1)}
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
      onReorder(arrayMove(ids, from, to));
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
