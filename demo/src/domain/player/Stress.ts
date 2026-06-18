export class Stress {
  private _value: number;

  static readonly MIN = 0;
  static readonly MAX = 100;

  constructor(initial: number) {
    this._value = Stress.clamp(initial);
  }

  get value(): number {
    return this._value;
  }

  increase(amount: number): void {
    if (amount < 0) throw new Error("Stress.increase: amount must be >= 0");
    this._value = Stress.clamp(this._value + amount);
  }

  decrease(amount: number): void {
    if (amount < 0) throw new Error("Stress.decrease: amount must be >= 0");
    this._value = Stress.clamp(this._value - amount);
  }

  apply(delta: number): void {
    this._value = Stress.clamp(this._value + delta);
  }

  isMaxed(): boolean {
    return this._value >= Stress.MAX;
  }

  private static clamp(v: number): number {
    return Math.max(Stress.MIN, Math.min(Stress.MAX, Math.round(v)));
  }
}