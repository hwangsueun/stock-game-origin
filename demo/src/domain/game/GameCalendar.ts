export type CalendarRow = {
  turn_index: number;
  game_date: string;          // "YYYY-MM-DD"
  is_repayment_trigger: boolean;
}

export class GameCalendar {
  private readonly rows: CalendarRow[];

  constructor(rows: CalendarRow[]) {
    if (rows.length === 0) throw new Error("GameCalendar: rows is empty");
    this.rows = [...rows].sort((a, b) => a.turn_index - b.turn_index);
  }

  getRow(turnIndex: number): CalendarRow {
    const row = this.rows.find((r) => r.turn_index === turnIndex);
    if (!row) throw new Error(`GameCalendar: no row for turn_index=${turnIndex}`);
    return row;
  }

  getDate(turnIndex: number): string {
    return this.getRow(turnIndex).game_date;
  }

  isRepaymentTrigger(turnIndex: number): boolean {
    return this.getRow(turnIndex).is_repayment_trigger;
  }

  get maxTurn(): number {
    return this.rows[this.rows.length - 1].turn_index;
  }

  get allRows(): CalendarRow[] {
    return [...this.rows];
  }
}
