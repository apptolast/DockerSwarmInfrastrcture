// Package guest dials a sandbox's guest ProcessService through
// atenet-router. It reimplements the part of AX's internal/guest.DialTarget
// the panel needs (google/ax f009cc8 internal/guest/client.go:45-60), which
// Go's internal-package rule keeps out of reach.
package guest

import (
	"context"
	"fmt"

	ateenvv1alpha "github.com/agent-substrate/env/proto/ateenv/v1alpha"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/metadata"
)

// TargetHeader routes a call to one actor (Substrate
// internal/atenet/headers.go:25-29).
const TargetHeader = "ate-target-actor"

// Client is a ProcessService client bound to one actor.
type Client struct {
	ateenvv1alpha.ProcessServiceClient
	conn *grpc.ClientConn
}

// Close releases the connection.
func (c *Client) Close() error { return c.conn.Close() }

// Dial connects to router over plain HTTP/2 inside the cluster and adds
// the actor header to every call. extra options are for tests.
func Dial(router, atespace, actor string, extra ...grpc.DialOption) (*Client, error) {
	if atespace == "" || actor == "" {
		return nil, fmt.Errorf("the task has no actor yet")
	}
	target := atespace + "/" + actor
	opts := append([]grpc.DialOption{
		grpc.WithTransportCredentials(insecure.NewCredentials()),
		grpc.WithChainUnaryInterceptor(func(ctx context.Context, method string,
			req, reply any, cc *grpc.ClientConn, invoker grpc.UnaryInvoker,
			opts ...grpc.CallOption) error {
			ctx = metadata.AppendToOutgoingContext(ctx, TargetHeader, target)
			return invoker(ctx, method, req, reply, cc, opts...)
		}),
		grpc.WithChainStreamInterceptor(func(ctx context.Context, desc *grpc.StreamDesc,
			cc *grpc.ClientConn, method string, streamer grpc.Streamer,
			opts ...grpc.CallOption) (grpc.ClientStream, error) {
			ctx = metadata.AppendToOutgoingContext(ctx, TargetHeader, target)
			return streamer(ctx, desc, cc, method, opts...)
		}),
	}, extra...)
	conn, err := grpc.NewClient(router, opts...)
	if err != nil {
		return nil, fmt.Errorf("dialing atenet-router: %w", err)
	}
	return &Client{ProcessServiceClient: ateenvv1alpha.NewProcessServiceClient(conn), conn: conn}, nil
}
