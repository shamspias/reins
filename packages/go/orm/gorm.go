package orm

import (
	"fmt"
	"sync"
	"time"

	reins "github.com/shamspias/reins/packages/go"
	"gorm.io/gorm"
	"gorm.io/gorm/schema"
)

// FromORM introspects GORM models and returns intent-level capabilities plus a
// schema index — the Go mirror of Python's Agent.from_orm. Reads are always
// generated; writes only when canWrite is true (and are gated by the autonomy
// ladder like any capability). Pass readLimit <= 0 for the default.
func FromORM(db *gorm.DB, models []any, canWrite bool, readLimit int) ([]reins.BoundCapability, reins.SchemaIndex, error) {
	if len(models) == 0 {
		return nil, reins.SchemaIndex{}, reins.NewError("no models given", "pass a slice of GORM model pointers, e.g. []any{&User{}, &Order{}}")
	}
	infos := make([]ModelInfo, 0, len(models))
	for _, m := range models {
		info, err := introspect(m)
		if err != nil {
			return nil, reins.SchemaIndex{}, err
		}
		infos = append(infos, info)
	}
	caps, index := BuildCapabilities(gormBackend{db: db}, infos, canWrite, readLimit)
	return caps, index, nil
}

// --- introspection via GORM's schema parser --------------------------------------

func introspect(model any) (ModelInfo, error) {
	parsed, err := schema.Parse(model, &sync.Map{}, schema.NamingStrategy{})
	if err != nil {
		return ModelInfo{}, reins.NewError(
			fmt.Sprintf("cannot introspect model %T: %v", model, err),
			"pass a GORM-mapped struct pointer",
		)
	}
	var columns []ColumnInfo
	var primaryKey *ColumnInfo
	for _, f := range parsed.Fields {
		if f.DBName == "" {
			continue // association / non-column field
		}
		col := ColumnInfo{
			Name:          f.DBName,
			JSONType:      jsonType(f.DataType),
			Nullable:      !f.NotNull && !f.PrimaryKey,
			Autoincrement: f.AutoIncrement,
			HasDefault:    f.HasDefaultValue || f.AutoCreateTime != 0 || f.AutoUpdateTime != 0,
			Unique:        f.Unique,
		}
		columns = append(columns, col)
		if f.PrimaryKey && primaryKey == nil {
			c := col
			primaryKey = &c
		}
	}
	return ModelInfo{
		Model:      model,
		Singular:   snake(parsed.Name),
		Plural:     parsed.Table,
		Columns:    columns,
		PrimaryKey: primaryKey,
	}, nil
}

func jsonType(dt schema.DataType) string {
	switch dt {
	case schema.Bool:
		return "boolean"
	case schema.Int, schema.Uint:
		return "integer"
	case schema.Float:
		return "number"
	default:
		return "string" // String, Time, Bytes, and anything else
	}
}

// --- the GORM backend ------------------------------------------------------------

type gormBackend struct {
	db *gorm.DB
}

func (g gormBackend) Find(info ModelInfo, filters map[string]any, limit int) ([]map[string]any, error) {
	var rows []map[string]any
	if err := g.db.Model(info.Model).Where(filters).Limit(limit).Find(&rows).Error; err != nil {
		return nil, err
	}
	return trimRows(info, rows), nil
}

func (g gormBackend) Count(info ModelInfo, filters map[string]any) (int, error) {
	var n int64
	if err := g.db.Model(info.Model).Where(filters).Count(&n).Error; err != nil {
		return 0, err
	}
	return int(n), nil
}

func (g gormBackend) Get(info ModelInfo, key string, value any) (map[string]any, error) {
	var rows []map[string]any
	if err := g.db.Model(info.Model).Where(map[string]any{key: value}).Limit(1).Find(&rows).Error; err != nil {
		return nil, err
	}
	if len(rows) == 0 {
		return nil, nil
	}
	return trimRow(info, rows[0]), nil
}

func (g gormBackend) Create(info ModelInfo, values map[string]any) (map[string]any, error) {
	if err := g.db.Model(info.Model).Create(values).Error; err != nil {
		return nil, err
	}
	return trimRow(info, values), nil
}

func (g gormBackend) Update(info ModelInfo, key string, value any, changes map[string]any) (map[string]any, error) {
	existing, err := g.Get(info, key, value)
	if err != nil {
		return nil, err
	}
	if existing == nil {
		return nil, missing(info, key, value)
	}
	if len(changes) > 0 {
		if err := g.db.Model(info.Model).Where(map[string]any{key: value}).Updates(changes).Error; err != nil {
			return nil, err
		}
	}
	return g.Get(info, key, value)
}

func (g gormBackend) Delete(info ModelInfo, key string, value any) error {
	existing, err := g.Get(info, key, value)
	if err != nil {
		return err
	}
	if existing == nil {
		return missing(info, key, value)
	}
	return g.db.Where(map[string]any{key: value}).Delete(info.Model).Error
}

// --- trimming --------------------------------------------------------------------

func trimRows(info ModelInfo, rows []map[string]any) []map[string]any {
	out := make([]map[string]any, 0, len(rows))
	for _, r := range rows {
		out = append(out, trimRow(info, r))
	}
	return out
}

func trimRow(info ModelInfo, row map[string]any) map[string]any {
	out := map[string]any{}
	for i, c := range info.Columns {
		if i >= maxColumnsReturned {
			break
		}
		out[c.Name] = jsonValue(row[c.Name])
	}
	return out
}

func jsonValue(v any) any {
	switch x := v.(type) {
	case []byte:
		return string(x)
	case time.Time:
		return x.Format(time.RFC3339)
	default:
		return v
	}
}

// --- name helper -----------------------------------------------------------------

func snake(name string) string {
	var out []rune
	for i, ch := range name {
		if ch >= 'A' && ch <= 'Z' {
			if i > 0 && !(name[i-1] >= 'A' && name[i-1] <= 'Z') {
				out = append(out, '_')
			}
			out = append(out, ch+('a'-'A'))
		} else {
			out = append(out, ch)
		}
	}
	return string(out)
}
